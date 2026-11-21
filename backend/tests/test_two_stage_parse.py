"""两段式解析：LLM 只出结构化事实，提示词由模板渲染。

背景（2026-11-21 改造）：
改造前 LLM 在单次调用里既做剧情拆解又写 image_prompt + video_prompt，
实测这两项占输出 44%（36506 字节里 16024 字），是撞网关 completion 上限
（间歇性 16000，reasoning 还吃掉 6k-12k）的直接原因。

实测对照（同一剧本、同一网关）：
| | 改造前 | 改造后 |
|---|---|---|
| LLM 写提示词 | 16024 字（44%） | 0 字 |
| 镜头数 | 7 | 14 |
| 调用次数 | 2（第 1 次截断） | 2（第 1 次截断，reasoning 波动） |

镜头数翻倍是关键收益：不再被提示词挤占输出预算，同一份剧本能拆出更多镜头。

本文件锁定三条不可回退的行为：
1. 解析提示词明确要求不输出 image_prompt / video_prompt；
2. normalize 阶段不伪造这两个字段（保持 None，由落库阶段渲染）；
3. 落库阶段必须渲染出非空提示词——下游 6 处依赖该字段。
"""
from __future__ import annotations

import pytest


def _raw_payload(with_prompts: bool = False) -> dict:
    shot = {
        "episode": 1, "seq": 1, "character_id": "苏晚", "props": ["帆布包"],
        "dialogue": "我们回家吧。",
        "dialogue_segments": [{"speaker": "苏晚", "text": "我们回家吧。",
                               "emotion": "sad", "order": 1}],
        "shot_kind": "dialogue", "duration": 8, "shot_type": "中景",
        "camera_angle": "平视", "camera_movement": "缓慢推近",
        "shot_purpose": "苏晚站在海边回望", "emotion": "sad",
        "beats": [{"action": "转身", "start_pose": "背对镜头",
                   "end_pose": "面向镜头", "duration": 2}],
        "start_state": {"pose": "背对立", "position": "画面中央", "expression": "平静",
                        "holding": "帆布包", "costume": "白裙", "injury": "无"},
        "end_state": {"pose": "面向站立", "position": "画面中央", "expression": "悲伤",
                      "holding": "帆布包", "costume": "白裙", "injury": "无"},
    }
    if with_prompts:
        shot["image_prompt"] = "LLM 写的分镜图提示词"
        shot["video_prompt"] = "LLM 写的视频提示词"
    return {
        "characters": [{"name": "苏晚", "description": "女主", "appearance": "长发白裙"}],
        "props": [{"name": "帆布包", "description": "随身包", "appearance": "米色棉麻"}],
        "scenes": [{
            "episode": 1, "title": "海边沙滩", "description": "夏日傍晚海滩",
            "time_note": "傍晚", "shots": [shot],
        }],
    }


# ---------- 1. 提示词要求 LLM 不输出这两项 ----------

def test_parse_system_prompt_forbids_prompt_fields():
    """生效的解析提示词必须明确禁止输出 image_prompt / video_prompt。

    注意：get_prompt 优先读 workspace/prompts/parse_system.md，
    只改代码里的内置默认值是无效的——这是实际踩过的坑。
    """
    from app.services.prompt_store import get_prompt

    p = get_prompt("parse_system")
    assert "不要输出 image_prompt" in p, (
        "生效提示词仍是旧版（要求 LLM 写提示词）。"
        "检查 workspace/prompts/parse_system.md 是否漏改。"
    )
    # 旧规则必须消失，否则模型仍会照着写
    assert "image_prompt 必须按结构化分段模板输出" not in p


# ---------- 2. normalize 不伪造字段 ----------

def test_normalize_leaves_prompts_none_when_llm_omits_them():
    """LLM 不产出时，normalize 不能凭空造一个，必须保持 None 交给落库阶段渲染。

    为什么不能让 normalize 渲染：normalize 是纯函数、不碰 DB，
    而渲染需要 prompt_store 模板与项目风格；职责分开更清晰。
    """
    from app.services.script_parse import normalize_parse_output

    norm = normalize_parse_output(_raw_payload(with_prompts=False),
                                  episode_start=1, episode_count=1)
    shot = norm.data["scenes"][0]["shots"][0]
    assert shot.get("image_prompt") is None
    assert shot.get("video_prompt") is None


def test_normalize_preserves_prompts_when_llm_provides_them():
    """兼容历史/外部数据：LLM 若仍给了提示词，不能被丢弃。"""
    from app.services.script_parse import normalize_parse_output

    norm = normalize_parse_output(_raw_payload(with_prompts=True),
                                  episode_start=1, episode_count=1)
    shot = norm.data["scenes"][0]["shots"][0]
    assert shot.get("image_prompt") == "LLM 写的分镜图提示词"
    assert shot.get("video_prompt") == "LLM 写的视频提示词"


# ---------- 3. 落库阶段必须渲染出可用提示词 ----------

def test_assemble_image_prompt_renders_from_structured_facts():
    """image_prompt 为空时按模板渲染，且必须覆盖关键分段。"""
    from app.services.script_parse import _assemble_image_prompt
    from app.services.script_parse import normalize_parse_output

    norm = normalize_parse_output(_raw_payload(with_prompts=False),
                                  episode_start=1, episode_count=1)
    shot = norm.data["scenes"][0]["shots"][0]
    rendered = _assemble_image_prompt(shot)

    assert rendered.strip(), "渲染结果不能为空（会下发空提示词）"
    # 结构化事实必须被带进提示词，否则等于丢失信息
    assert "苏晚站在海边回望" in rendered, "shot_purpose 未进入提示词"
    assert "转身" in rendered, "beats 动作未进入提示词"
    assert "中景" in rendered, "景别未进入提示词"
    assert "帆布包" in rendered, "道具未进入提示词"
    # 模板固定分段
    assert "必须保持" in rendered
    assert "禁止" in rendered


def test_assemble_image_prompt_handles_missing_optional_fields():
    """缺 beats / state / props 时不能抛异常，要给出占位而不是崩。"""
    from app.services.script_parse import _assemble_image_prompt

    minimal = {"episode": 1, "seq": 1}
    rendered = _assemble_image_prompt(minimal)
    assert rendered.strip()
    assert "第 1-1 镜" in rendered


def test_assemble_video_prompt_still_works():
    """video_prompt 的兜底机制本来就存在，改造不能破坏它。"""
    from app.services.script_parse import _assemble_video_prompt, normalize_parse_output

    norm = normalize_parse_output(_raw_payload(with_prompts=False),
                                  episode_start=1, episode_count=1)
    shot = norm.data["scenes"][0]["shots"][0]
    rendered = _assemble_video_prompt(shot, is_first=True)
    assert rendered.strip()
    assert "第 1-1 镜" in rendered
