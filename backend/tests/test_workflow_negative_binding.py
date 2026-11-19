"""工作流级回归测试：图片负向提示词绑定 + 多参考槽位容量。

背景（场景纯净度根因）：
- qwen21 图片工作流（multi 与 t2i）的 TextEncode 节点 negative_prompt 原为空字符串且未声明
  $negative.negative_prompt 标记，资产侧写入的 negative（没有人/没有文字等
  五层污染否定词）在 inject_params 中被静默丢弃，模型从未收到否定约束。
- wf_image_ref_qwen21_multi 原仅 7 槽（ref_character_4 封顶），视频侧已到 9 槽，
  导致第 5/6 个角色在关键帧阶段拿不到参考图。
"""
import json
from pathlib import Path

from app.services.workflow_inject import inject_params, parse_workflow
from app.services.workflow.capabilities import normalize as normalize_capabilities

WORKFLOW_DIR = Path(__file__).resolve().parents[1] / "data" / "workflows"
# 单角色版 wf_image_ref_qwen21_turbo 已下线：multi 是它的超集且为默认工作流，
# 只传 3 张参考图时 multi 会自动剔除多余槽位，行为与旧单角色版等价。
IMAGE_WORKFLOWS = [
    "wf_image_ref_qwen21_multi",
    "wf_image_t2i_qwen21_turbo",
]


def _load(name: str) -> dict:
    return json.loads((WORKFLOW_DIR / f"{name}.json").read_text("utf-8"))


def _caps(name: str) -> dict:
    """从工作流定义自动解析能力（不再依赖 .meta.json）。"""
    definition = _load(name)
    return normalize_capabilities("unknown", definition, capabilities=None, trigger=None)


def test_image_workflows_bind_negative():
    """图片工作流必须声明 $negative 参数，否则否定约束无法送达模型。"""
    for name in IMAGE_WORKFLOWS:
        parsed = parse_workflow(_load(name))
        assert parsed.errors == [], f"{name} 标记校验失败: {parsed.errors}"
        assert "negative" in parsed.params, f"{name} 未绑定 $negative.negative_prompt"


def test_negative_actually_injected_into_text_encode():
    """negative 必须真正写进文本编码节点，而不是只停留在 params。"""
    negative = "没有任何人，没有文字与装饰"
    for name in IMAGE_WORKFLOWS:
        wf = _load(name)
        parsed = parse_workflow(wf)
        out, snapshot = inject_params(wf, {"prompt": "P", "negative": negative})
        assert snapshot.get("negative") == negative, f"{name} 注入快照缺失 negative"
        node_id = parsed.params["negative"].node_id
        assert out[node_id]["inputs"]["negative_prompt"] == negative, (
            f"{name} negative 未写入文本编码节点"
        )


def test_multi_workflow_has_nine_reference_slots():
    """multi 工作流需与视频侧对齐到 9 槽，覆盖第 5/6 个角色。"""
    caps = _caps("wf_image_ref_qwen21_multi")
    params = [s["param"] if isinstance(s, dict) else s for s in caps["reference"]["slots"]]
    assert len(params) == 9, params
    assert "ref_character_5" in params and "ref_character_6" in params
    assert caps["reference"]["max_image_references"] == 9


def test_multi_workflow_declares_load_image_nodes_for_all_slots():
    """每个声明槽位都要有对应 LoadImage 节点，且能在解析结果中识别为媒体槽。"""
    parsed = parse_workflow(_load("wf_image_ref_qwen21_multi"))
    caps = _caps("wf_image_ref_qwen21_multi")
    media_slots = {k for k, v in parsed.params.items() if v.media}
    for item in caps["reference"]["slots"]:
        param = item["param"] if isinstance(item, dict) else item
        assert param in media_slots, f"槽位 {param} 未在定义中声明为媒体槽"
    assert len(media_slots) == 9, sorted(media_slots)
