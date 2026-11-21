"""回归测试：分镜视频提示词的 <Picture N> 必须与实际注入的参考图槽位一致。

真实事故（「海风与夏日」第 2 集，分镜看板 → 分镜视频详情）：

- 页面「参考图」只有 3 张（本镜分镜图 + 林晚 + 江屿），没有上一镜尾帧；
- 提示词却写着「<Picture 1>：上一镜头尾帧」「<Picture 2>：当前镜头分镜图」；
- 两个角色定妆图（静态第 3/4 张）在提示词里没有任何描述；
- 分镜图在页面上标 <Picture 2>，但它其实是模型收到的第 1 张。

## 根因（三层，全部是「编号口径」分叉）

1. 模板 `video_prompt_body.md` 把图序写死成 1=尾帧、2=分镜图，没有角色图；
2. 工作流静态声明是 1..9 个槽位，页面直接展示静态 `image_index`，
   但执行器会把空槽位剔除并**重新连续编号**（compact_reference_slots）；
3. 上一镜视频还没生成 → `ref_prev_frame` 缺失 → 分镜图从静态第 2 张
   变成实际第 1 张，提示词里的「Picture 1 = 尾帧」指向一张不存在的图。

修复后唯一口径：`reference_layout.compressed_numbers()`
（实际有图的槽位按声明顺序压缩成 1..N），页面展示、提示词文本、提交校验三处共用。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.services.reference_layout import (
    build_duty_block,
    compressed_numbers,
    rebuild_prompt,
    slot_kind,
    static_numbers,
)

# 与 H3 ref2va 工作流真实声明一致（9 个图片槽位，ref_image_N 为 0-based → Picture N+1）
DECLARED = [
    {"param": "ref_prev_frame", "input_key": "ref_images.ref_image_0"},
    {"param": "ref_image", "input_key": "ref_images.ref_image_1"},
    {"param": "ref_identity", "input_key": "ref_images.ref_image_2"},
    {"param": "ref_character_2", "input_key": "ref_images.ref_image_3"},
    {"param": "ref_prop", "input_key": "ref_images.ref_image_4"},
    {"param": "ref_character_3", "input_key": "ref_images.ref_image_5"},
    {"param": "ref_character_4", "input_key": "ref_images.ref_image_6"},
    {"param": "ref_character_5", "input_key": "ref_images.ref_image_7"},
    {"param": "ref_character_6", "input_key": "ref_images.ref_image_8"},
]

NAMES = {"ref_image": "storyboard_16_img", "ref_identity": "林晚", "ref_character_2": "江屿"}


def _template() -> str:
    """读真实模板，确保测试锁的是线上文本而不是测试自造的假文本。"""
    path = Path(__file__).resolve().parents[1] / "workspace" / "prompts" / "video_prompt_body.md"
    return path.read_text(encoding="utf-8").replace("{episode}", "2").replace("{seq}", "2")


def _tags(text: str) -> set[int]:
    return {int(m.group(1)) for m in re.finditer(r"<Picture\s*(\d+)>", text)}


def test_static_numbers_are_sparse_but_compressed_are_contiguous():
    """静态编号有断档（尾帧缺 → 2/3/4），压缩后必须是 1/2/3 连续。"""
    static = static_numbers(DECLARED)
    assert [static[s] for s in ("ref_prev_frame", "ref_image", "ref_identity", "ref_character_2")] == [1, 2, 3, 4]

    numbers = compressed_numbers(["ref_image", "ref_identity", "ref_character_2"], DECLARED)
    assert numbers == {"ref_image": 1, "ref_identity": 2, "ref_character_2": 3}


def test_missing_prev_frame_does_not_leave_dangling_picture_1():
    """上一镜没有尾帧时，提示词不能再说「Picture 1 是上一镜头尾帧」。"""
    numbers = compressed_numbers(["ref_image", "ref_identity", "ref_character_2"], DECLARED)
    out = rebuild_prompt(_template(), numbers, NAMES, DECLARED, "picture")

    # 1. 所有引用都落在真实存在的 1..3
    assert _tags(out) == {1, 2, 3}
    # 2. 不再有指向尾帧的职责行
    assert "【<Picture 1>：上一镜头尾帧】" not in out
    assert "承接上一镜头尾帧 <Picture 1>" not in out
    assert "按剧情与分镜图推断" in out
    # 3. 两个角色的定妆图必须被描述（此前完全缺失）
    assert "- <Picture 2>（角色 @林晚 的定妆图）" in out
    assert "- <Picture 3>（角色 @江屿 的定妆图）" in out
    # 4. 分镜图必须落到第 1 张（模型实际收到的顺序）
    assert "- <Picture 1>（当前镜头分镜图）" in out
    assert "只还原 <Picture 1> 的构图" in out


def test_with_prev_frame_keeps_frame_semantics_in_order():
    """有上一镜尾帧时：1=尾帧、2=分镜图、3/4=角色，承接句必须还指向尾帧。"""
    numbers = compressed_numbers(
        ["ref_prev_frame", "ref_image", "ref_identity", "ref_character_2"], DECLARED
    )
    assert numbers == {
        "ref_prev_frame": 1,
        "ref_image": 2,
        "ref_identity": 3,
        "ref_character_2": 4,
    }
    out = rebuild_prompt(_template(), numbers, NAMES, DECLARED, "picture")

    assert _tags(out) == {1, 2, 3, 4}
    assert "- <Picture 1>（上一镜头尾帧）" in out
    assert "- <Picture 2>（当前镜头分镜图）" in out
    assert "承接上一镜头尾帧 <Picture 1>" in out
    assert "只还原 <Picture 2> 的构图" in out
    assert "没有可用的上一镜尾帧" not in out


def test_rebuild_is_idempotent():
    """详情页会被反复读取，重建必须幂等，否则第二次就把正确引用删了。"""
    numbers = compressed_numbers(["ref_image", "ref_identity", "ref_character_2"], DECLARED)
    once = rebuild_prompt(_template(), numbers, NAMES, DECLARED, "picture")
    twice = rebuild_prompt(once, numbers, NAMES, DECLARED, "picture")
    assert once == twice


def test_duty_block_lists_every_injected_slot():
    """职责块必须逐张列出实际注入的图，且编号连续无洞。"""
    numbers = compressed_numbers(
        ["ref_prev_frame", "ref_image", "ref_identity", "ref_character_2", "ref_prop"], DECLARED
    )
    names = {**NAMES, "ref_prop": "草帽"}
    lines = build_duty_block(numbers, names, "picture")
    tagged = [int(m.group(1)) for line in lines for m in re.finditer(r"<Picture\s*(\d+)>", line)]
    assert sorted(tagged) == [1, 2, 3, 4, 5]
    assert any("草帽" in line for line in lines)
    assert any("角色站位固定" in line for line in lines)


def test_slot_kind_covers_all_character_slots():
    for slot in ("ref_identity", "ref_character_2", "ref_character_6"):
        assert slot_kind(slot)[0] == "character"
    assert slot_kind("ref_prev_frame")[0] == "prev_frame"
    assert slot_kind("ref_image")[0] == "keyframe"


# ── 执行期图序过期：尾帧后到 / 尾帧被 pop（executor 侧的真实场景）──────────────


def test_late_prev_frame_shifts_every_number_and_keeps_idempotent():
    """提交时无尾帧、执行时补上：所有编号整体后移一位，承接句要还原。

    真实链路：提交时上一镜还没出视频 → 提示词降级为「按剧情与分镜图推断」；
    执行器 chained / last_frame_asset_id 注入 ref_prev_frame 后，
    模型收到的第 1 张变成尾帧，分镜图退到第 2 张。旧实现只用 remap_picture_refs
    删行，不会插入+整体平移，于是提示词继续把第 1 张当分镜图。
    """
    submitted = compressed_numbers(["ref_image", "ref_identity", "ref_character_2"], DECLARED)
    p1 = rebuild_prompt(_template(), submitted, NAMES, DECLARED, "picture")
    assert "按剧情与分镜图推断" in p1

    late = compressed_numbers(
        ["ref_prev_frame", "ref_image", "ref_identity", "ref_character_2"], DECLARED
    )
    p2 = rebuild_prompt(p1, late, NAMES, DECLARED, "picture")

    assert _tags(p2) == {1, 2, 3, 4}
    assert "- <Picture 1>（上一镜头尾帧）" in p2
    assert "- <Picture 2>（当前镜头分镜图）" in p2
    assert "开场状态（承接上一镜头尾帧 <Picture 1>）" in p2
    assert "只还原 <Picture 2> 的构图" in p2
    assert "按剧情与分镜图推断" not in p2
    # 占位符不能泄漏到最终文本
    assert "\x00" not in p2
    # 再跑一次必须不变（详情页/重试会反复经过同一函数）
    assert p2 == rebuild_prompt(p2, late, NAMES, DECLARED, "picture")


def test_prev_frame_popped_at_runtime_degrades_again():
    """independent 模式会在执行期 pop 掉 ref_prev_frame：编号要收回并重新降级。"""
    with_prev = compressed_numbers(
        ["ref_prev_frame", "ref_image", "ref_identity", "ref_character_2"], DECLARED
    )
    p_with = rebuild_prompt(_template(), with_prev, NAMES, DECLARED, "picture")

    without = compressed_numbers(["ref_image", "ref_identity", "ref_character_2"], DECLARED)
    p_without = rebuild_prompt(p_with, without, NAMES, DECLARED, "picture")

    assert _tags(p_without) == {1, 2, 3}
    assert "上一镜头尾帧" not in p_without
    assert "按剧情与分镜图推断" in p_without
    assert p_without == rebuild_prompt(p_without, without, NAMES, DECLARED, "picture")


def test_names_survive_windows_style_paths():
    """media 里的路径可能是 `data\\media\\x.png`（历史数据混用分隔符）。

    执行器按 file_path 等值查资产会匹配不到，职责块就退化成「@角色」，
    模型拿不到角色名只能自由发挥长相。断言：归一化后名字必须还在。
    """
    import os

    numbers = compressed_numbers(["ref_image", "ref_identity"], DECLARED)
    # 模拟执行器侧的名字解析：basename 归一化
    media = {
        "ref_image": "data\\media\\storyboards\\storyboard_16_img.png",
        "ref_identity": "data/media/ast_4cb74a034794_v1.png",
    }
    assets = [
        ("data/media/storyboards/storyboard_16_img.png", "storyboard_16_img"),
        ("data\\media\\ast_4cb74a034794_v1.png", "林晚"),
    ]
    want = {os.path.basename(v.replace("\\", "/")): k for k, v in media.items()}
    names = {}
    for path, name in assets:
        slot = want.get(os.path.basename(path.replace("\\", "/")))
        if slot:
            names[slot] = name
    assert names == {"ref_image": "storyboard_16_img", "ref_identity": "林晚"}

    out = rebuild_prompt(_template(), numbers, names, DECLARED, "picture")
    assert "（角色 @林晚 的定妆图）" in out
    assert "@角色" not in out


def test_h3_three_section_prompt_keeps_duty_block_inside_container():
    """真实历史数据格式（`integrated_multimodal_description:` 开头）。

    H3 只从这一行**之后**读画面描述，职责块插到它之前等于没写。
    同时这种历史写法用的是 `- Picture 1：上一镜头尾帧，…`（无尖括号），
    必须被识别并替换，否则同一张图会同时存在两套矛盾的职责描述。
    """
    prompt = "\n".join([
        "integrated_multimodal_description:",
        "# 第1集第10镜｜MiniMax H3 I2VA 动态视频提示词",
        "",
        "【参考图职责与优先级】",
        "- Picture 1：上一镜头尾帧，仅用于人物姿态参考。不得作为首帧。",
        "- Picture 2：当前镜头分镜图，是本镜头唯一的构图基准。",
        "- 两张参考图发生视觉冲突时，以 Picture 2 为准。",
        "",
        "【任务类型】",
        "I2VA，生成第1集第10镜动态视频。",
        "",
        "overall_soundscape: 海浪声",
        "",
        "non_diegetic_music: N/A",
    ])
    numbers = compressed_numbers(
        ["ref_prev_frame", "ref_image", "ref_identity"], DECLARED
    )
    names = {"ref_image": "storyboard", "ref_identity": "苏晚"}
    out = rebuild_prompt(prompt, numbers, names, DECLARED, "picture")

    lines = out.split("\n")
    container = next(i for i, l in enumerate(lines) if l.startswith("integrated_multimodal_description"))
    duty = next(i for i, l in enumerate(lines) if l.startswith("【参考图职责】"))
    assert duty > container, "职责块必须在 H3 正文容器之后，否则模型读不到"

    # 旧的无尖括号写法必须被清掉，不能与新区块并存
    assert "- Picture 1：上一镜头尾帧" not in out
    assert "- Picture 2：当前镜头分镜图" not in out
    # 角色定妆图必须被描述（旧写法里完全没有）
    assert "- <Picture 3>（角色 @苏晚 的定妆图）" in out
    # 三段式字段不能被破坏
    assert "overall_soundscape:" in out and "non_diegetic_music:" in out
    # 幂等（含空行）
    assert out == rebuild_prompt(out, numbers, names, DECLARED, "picture")


def test_at_character_is_bound_to_a_picture_number():
    """`@角色` 必须在职责块里明确「正文中的 @名字 就是第几张图」。

    修复前提示词里只有字面的 `画面主体为 @苏晚。`，全仓没有任何代码把 @名字
    映射成图片编号 —— 模型看到「@苏晚」不知道指的是 Picture 3 那张定妆图，
    只能靠角色名自由发挥长相。槽位给对了也没用。
    """
    numbers = compressed_numbers(["ref_image", "ref_identity"], DECLARED)
    names = {"ref_image": "storyboard", "ref_identity": "林晚"}
    out = rebuild_prompt(_template(), numbers, names, DECLARED, "picture")
    assert "- <Picture 2>（角色 @林晚 的定妆图）：正文中 @林晚 即指这张图。" in out


# ── 尾帧解析：Windows 反斜杠路径 ────────────────────────────────────────


def test_normalize_path_handles_windows_separators():
    """库里 26/26 个 last_frame 资产都是 `data\\media\\...` 反斜杠路径。

    Linux 上 os.path.exists('data\\media\\x.png') 恒为 False →
    resolve_prev_frame() 永远返回 None → 串行链式「上一镜尾帧→下一镜首帧」
    这条链从来没接上过，而任务照样成功，只是模型拿不到连续性参考。
    """
    from app.services.shot_chain import _normalize_path

    assert _normalize_path("data\\media\\last_frames\\sht_1.png") == "data/media/last_frames/sht_1.png"
    assert _normalize_path("data/media/x.png") == "data/media/x.png"
    assert _normalize_path(None) == ""
    assert _normalize_path("") == ""
