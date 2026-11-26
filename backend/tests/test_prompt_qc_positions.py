"""站位校验回归：站位句用裸名，不能只认 @引用。

真实故障（镜 7/9，prj_a3fa93b8eb7f）：提示词里明明写了
「- 林小满：画面左侧中景，站在收银台后方」，但 `_sentence_around` 只匹配
`@林小满`，唯一命中的是首行「参考资产：@林小满 @陈默 @便利店」——那里当然没有
位置词，于是判 `character_position_missing`。误判代价是触发一次 LLM 改写
（白烧一次请求），改写失败则 409 阻断出图；镜 9 最终只画出 1 人（期望 2 人）。
"""

from __future__ import annotations

from app.services.prompt_qc import _POSITION_TOKENS, _position_sentences


def test_position_sentence_matched_by_bare_name():
    text = (
        "参考资产：@林小满 @陈默 @便利店\n"
        "任务：生成第 1 集第 9 镜的单帧分镜图\n"
        "【出场角色与站位】林小满：画面左侧中景，站在收银台后方；陈默：画面右侧后景，坐在窗边\n"
    )
    segs = _position_sentences(text, "林小满")
    assert segs, "裸名站位句没有被收集"
    assert any(tok in s for s in segs for tok in _POSITION_TOKENS), "站位句应命中位置词"


def test_ref_declaration_sentence_excluded():
    """「参考资产」清单行只声明注入了哪些图，不能被当成站位句。"""
    text = "参考资产：@林小满 @陈默\n"
    assert _position_sentences(text, "林小满") == []


def test_scene_anchored_positions_are_accepted():
    """场景锚点位置（后方/窗边/柜台后）也是明确站位，不能判缺失。"""
    cases = [
        "林小满：位于收银台后方",
        "陈默：坐在窗边座位上",
        "店员：站在柜台后",
        "两人：画面深处",
    ]
    for case in cases:
        assert any(tok in case for tok in _POSITION_TOKENS), f"位置词表缺：{case}"


def test_really_missing_position_still_detected():
    """真的没给方位时仍要报出来（镜 7 陈默：「相对方位以分镜图参考图为准」）。"""
    text = "陈默：与林小满同处本镜头画幅内，相对方位以分镜图参考图为准，不得与林小满互换左右前后\n"
    segs = _position_sentences(text, "陈默")
    assert segs
    assert not any(tok in s for s in segs for tok in _POSITION_TOKENS), "含糊描述不应被当成站位"
