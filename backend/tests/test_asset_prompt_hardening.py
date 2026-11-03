"""第 3 步：资产图提示词硬化与场景纯净度闭环测试。

覆盖：
- 角色模板三视图交付规范（同一角色三视角、身高对齐、共用光照）
- 场景模板五类污染源分层约束（人影/间接人影/动物载具/剧情道具/文字）
- 道具模板多视角与纯净度约束
- 底图卫兵：质检 failed 的资产不得作为下游参考，pending 不误伤
- 场景板接线：plate_text 优先于 description，且不拼接第四行风格
- 地点圣经：同地点 anchors 复用
- persist_fallback：规则兜底不落库，避免固化粗清洗结果
"""
import json
import os
import tempfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Asset, Base, Scene


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()


# ---------- 模板硬化 ----------
def test_character_template_three_view_spec():
    from app.services.prompt_compiler import CHARACTER_TEMPLATE

    # 单视图为默认交付，不再要求三视图旧词
    assert "默认只输出一张正面全身定妆照" in CHARACTER_TEMPLATE
    assert "不做并排多视角拼图" in CHARACTER_TEMPLATE
    assert "头顶到脚底全部可见" in CHARACTER_TEMPLATE
    assert "只有一个视角" in CHARACTER_TEMPLATE
    assert "不同角色同框" in CHARACTER_TEMPLATE
    assert "不生成表情合集" in CHARACTER_TEMPLATE


def test_scene_template_five_pollution_layers():
    from app.services.prompt_compiler import SCENE_TEMPLATE

    assert "没有任何人" in SCENE_TEMPLATE
    assert "没有间接人影" in SCENE_TEMPLATE
    assert "倒影" in SCENE_TEMPLATE and "海报" in SCENE_TEMPLATE
    assert "没有动物与载具" in SCENE_TEMPLATE
    assert "没有剧情道具" in SCENE_TEMPLATE
    assert "没有文字与装饰" in SCENE_TEMPLATE


def test_prop_template_view_and_purity():
    from app.services.prompt_compiler import PROP_TEMPLATE

    assert "正视与侧视" in PROP_TEMPLATE
    assert "材质必须可辨识" in PROP_TEMPLATE
    assert "没有使用场景" in PROP_TEMPLATE
    assert "不出现角色" in PROP_TEMPLATE


# ---------- 底图卫兵 ----------
def _mk_asset(db, aid, quality_status):
    f = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    f.write(b"x" * 16)
    f.close()
    db.add(Asset(id=aid, project_id="p", asset_type="scene", name="场景",
                 version=1, file_path=f.name, validity="current",
                 quality_status=quality_status))
    db.commit()
    return f.name


def test_guard_blocks_failed_asset(db):
    from app.services.prompt_compiler import _approved_asset

    path = _mk_asset(db, "a_fail", "failed")
    try:
        asset, issue = _approved_asset(db, "a_fail", "p", ("scene",))
        assert asset is None
        assert issue["code"] == "reference_asset_quality_failed"
    finally:
        os.unlink(path)


def test_guard_allows_pending_and_passed(db):
    from app.services.prompt_compiler import _approved_asset

    for aid, qs in (("a_pending", "pending"), ("a_passed", "passed"), ("a_none", None)):
        path = _mk_asset(db, aid, qs)
        try:
            asset, issue = _approved_asset(db, aid, "p", ("scene",))
            assert asset is not None, qs
            assert issue is None
        finally:
            os.unlink(path)


# ---------- 场景板接线 ----------
def test_scene_prompt_prefers_plate_and_drops_style_line():
    from app.services.asset_plan import _scene_prompt

    s = Scene(id="s1", project_id="p", episode=1, seq=1, title="马棚",
              description="小马跑进马棚找妈妈。",
              plate_text=("清晨晴，空无一人的木马棚只由立柱与食槽构成。\n"
                          "右上直射光，暖白。\n"
                          "9:16 竖构图，24mm。\n"
                          "中性写实风格。"))
    p = _scene_prompt(s, ["小马"])
    assert "空无一人" in p
    assert "中性写实风格" not in p
    assert "。，" not in p


def test_scene_prompt_falls_back_without_plate():
    from app.services.asset_plan import _scene_prompt

    s = Scene(id="s2", project_id="p", episode=1, seq=1, title="河边",
              description="清澈的河水，两岸绿草。")
    assert "空场景基准图" in _scene_prompt(s, [])


# ---------- 地点圣经 ----------
def test_location_bible_reuses_anchors(db):
    from app.services.scene_plate import _location_bible

    db.add(Scene(id="s1", project_id="p", episode=1, seq=1, title="马棚",
                 normalized_name="马棚|清晨",
                 plate=json.dumps({"location_id": "L1", "location_name": "马棚",
                                   "anchors": ["木栅栏", "石槽"]}, ensure_ascii=False)))
    db.commit()

    hit = _location_bible(db, "p", "马棚|清晨")
    assert "木栅栏" in hit and "石槽" in hit
    assert "木栅栏" in _location_bible(db, "p", "L1")
    assert _location_bible(db, "p", "河边|正午") == "（无，请在本条内首次定义锚点）"
    assert _location_bible(db, "p", None) == "（无，请在本条内首次定义锚点）"


# ---------- 兜底不落库 ----------
def test_persist_fallback_false_does_not_write(db, monkeypatch):
    from app.services import scene_plate as sp

    def _boom(*a, **k):
        raise sp.LLMError("llm_unconfigured", "forced")

    monkeypatch.setattr(sp, "_call_llm", _boom)

    s = Scene(id="s1", project_id="p", episode=1, seq=1, title="马棚",
              description="木质马棚，清晨斜光。")
    db.add(s)
    db.commit()

    r = sp.compile_scene_plate(db, s, persist_fallback=False)
    db.refresh(s)
    assert r["source"] == "rules_transient"
    assert not s.plate_text


def test_persist_fallback_true_writes(db, monkeypatch):
    from app.services import scene_plate as sp

    def _boom(*a, **k):
        raise sp.LLMError("llm_unconfigured", "forced")

    monkeypatch.setattr(sp, "_call_llm", _boom)

    s = Scene(id="s2", project_id="p", episode=1, seq=1, title="河边",
              description="清澈的河水，两岸绿草。")
    db.add(s)
    db.commit()

    r = sp.compile_scene_plate(db, s, persist_fallback=True)
    db.refresh(s)
    assert r["source"] == "rules"
    assert s.plate_text
