"""P4/G1/G2：工作流匹配器 + 项目级默认工作流。

对应 docs/plan/workflow-dynamic-audit.md 附录 E.4：
- G1 无 matcher 服务（`capability_matches` 无调用方）→ 显式不兼容工作流必须拦截
- G2 无项目级默认工作流 → `project.settings.default_workflows`，不建新表
- §24 第 16 条：用户明确指定的工作流不可用时，不得静默回退到其他工作流

全部用例驱动真实工作流 JSON（`backend/data/workflows/`），不使用虚构节点。
"""
import json
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Base
from app.models.narrative import Project
from app.models.production import Workflow, ids
from app.services import errors
from app.services.workflow import matcher
from app.services.workflow.capabilities import normalize as normalize_capabilities

WF_DIR = Path(__file__).resolve().parents[1] / "data" / "workflows"


def _load(name: str) -> tuple[dict, dict]:
    definition = json.loads((WF_DIR / f"{name}.json").read_text(encoding="utf-8"))
    # 自动生成能力与触发条件（不再依赖 .meta.json）
    caps = normalize_capabilities("unknown", definition, capabilities=None, trigger=None)
    wf_type = caps.get("workflow_type", "unknown")
    _trigger_targets = {"image": "keyframe", "video": "clip"}
    trigger = {"target": _trigger_targets.get(wf_type, wf_type), "required_params": [], "priority": 10}
    meta = {"capabilities": caps, "trigger": trigger, "workflow_type": wf_type}
    return definition, meta


@pytest.fixture()
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()


def _add_workflow(db, name: str, *, workflow_type: str, is_default=False,
                  is_builtin=False, caps_override: dict | None = None) -> Workflow:
    definition, meta = _load(name)
    caps = dict(meta.get("capabilities") or {})
    if caps_override:
        caps.update(caps_override)
    from datetime import datetime, timezone
    wf = Workflow(
        id=ids.workflow_id(),
        name=name,
        alias=name,
        workflow_type=workflow_type,
        definition=definition,
        capabilities=caps,
        trigger=meta.get("trigger") or {},
        version=1,
        is_default=is_default,
        is_builtin=is_builtin,
        created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        updated_at=datetime.now(timezone.utc).replace(tzinfo=None),
    )
    db.add(wf)
    db.commit()
    return wf


def _add_project(db, settings: dict | None = None) -> Project:
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    p = Project(
        id=ids.project_id(),
        name="匹配测试项目",
        aspect_ratio="9:16",
        settings=settings or {},
        created_at=now,
        updated_at=now,
    )
    db.add(p)
    db.commit()
    return p


# --- 基础匹配 -------------------------------------------------------------

def test_capability_matches_real_h3_declares_audio_input(db):
    """真实 H3 工作流的归一化能力必须声明音频输入（从 ref_audio_N 标记读，不读名字）。"""
    wf = _add_workflow(db, "wf_video_h3_ref2va_std_5s", workflow_type="video")
    res = matcher.resolve(
        db, slot="video",
        extra_required={"modalities.audio_input": True, "audio.accepts_tts_reference_audio": True},
    )
    assert res.workflow is not None, f"应命中 H3，missing={res.missing}"
    assert res.workflow.id == wf.id
    caps = matcher._caps_of(wf)
    assert caps["modalities"]["audio_input"] is True
    assert caps["audio"]["accepts_tts_reference_audio"] is True
    assert caps["audio"]["max_audio_inputs"] == 3


def test_t2i_workflow_lacks_audio_input_so_capability_check_fails(db):
    """纯文生图工作流不声明音频输入——能力检查必须真的拒绝，而不是看类型通过。"""
    wf = _add_workflow(db, "wf_image_t2i_qwen21_turbo", workflow_type="image")
    ok, missing = matcher.caps_svc.capability_matches(
        matcher._caps_of(wf), {"modalities.audio_input": True}
    )
    assert ok is False
    assert missing == ["modalities.audio_input"]


def test_resolve_picks_workflow_with_required_capability_only(db):
    """两个 video 工作流，只有 H3 有音频输入：要求音频时必须选 H3，不能按创建顺序取第一个。"""
    t2v = _add_workflow(db, "wf_video_h3_t2v", workflow_type="video")   # 无 ref 槽位
    h3 = _add_workflow(db, "wf_video_h3_ref2va_std_5s", workflow_type="video")
    res = matcher.resolve(
        db, slot="video", extra_required={"modalities.audio_input": True}
    )
    assert res.workflow is not None
    assert res.workflow.id == h3.id
    assert res.workflow.id != t2v.id
    assert res.source == "capability_match"


def test_resolve_returns_missing_capabilities_when_none_match(db):
    """没有候选满足时返回缺失点列表，调用方据此给出可操作报错。"""
    _add_workflow(db, "wf_image_t2i_qwen21_turbo", workflow_type="image")
    res = matcher.resolve(
        db, slot="image", extra_required={"modalities.audio_input": True}
    )
    assert res.workflow is None
    assert "modalities.audio_input" in res.missing
    with pytest.raises(Exception) as exc:
        res.require(slot="image")
    detail = exc.value.detail  # type: ignore[attr-defined]
    assert detail["code"] == "workflow_capability_mismatch"
    assert detail["missing_capabilities"] == ["modalities.audio_input"]


# --- 显式指定：不得静默回退（§24 第 16 条） --------------------------------

def test_explicit_workflow_missing_capability_raises_not_fallback(db):
    """用户明确指定了不支持音频的 T2V，必须报错，不能偷偷换成 H3。"""
    t2v = _add_workflow(db, "wf_video_h3_t2v", workflow_type="video")
    _add_workflow(db, "wf_video_h3_ref2va_std_5s", workflow_type="video")  # 更好的候选
    with pytest.raises(Exception) as exc:
        matcher.resolve(
            db, slot="video", workflow_id=t2v.id,
            extra_required={"modalities.audio_input": True},
        )
    detail = exc.value.detail  # type: ignore[attr-defined]
    assert detail["code"] == "workflow_capability_mismatch"
    assert detail["workflow_id"] == t2v.id


def test_explicit_workflow_wrong_type_raises(db):
    img = _add_workflow(db, "wf_image_t2i_qwen21_turbo", workflow_type="image")
    with pytest.raises(Exception) as exc:
        matcher.resolve(db, slot="video", workflow_id=img.id)
    detail = exc.value.detail  # type: ignore[attr-defined]
    assert detail["code"] == "workflow_capability_mismatch"
    assert "image" in detail["message"]


def test_explicit_workflow_not_found_raises(db):
    with pytest.raises(Exception) as exc:
        matcher.resolve(db, slot="video", workflow_id="wf_does_not_exist")
    assert exc.value.detail["code"] == "workflow_not_found"  # type: ignore[attr-defined]


def test_explicit_workflow_satisfying_caps_is_used(db):
    """显式指定且能力满足 → 直接用，不做二次挑选。"""
    h3 = _add_workflow(db, "wf_video_h3_ref2va_std_5s", workflow_type="video")
    res = matcher.resolve(
        db, slot="video", workflow_id=h3.id,
        extra_required={"modalities.audio_input": True},
    )
    assert res.source == "explicit"
    assert res.workflow.id == h3.id  # type: ignore[union-attr]


# --- 项目级默认工作流 -----------------------------------------------------

def test_project_default_takes_precedence_over_system_default(db):
    """项目默认优先于系统默认：同一类型下两条都满足能力时选项目默认。"""
    sys_default = _add_workflow(db, "wf_video_h3_ref2va_fast_5s", workflow_type="video", is_default=True)
    proj_pick = _add_workflow(db, "wf_video_h3_ref2va_quality_10s", workflow_type="video")
    project = _add_project(db, {"default_workflows": {"video": proj_pick.id}})
    res = matcher.resolve(db, slot="video", project_id=project.id)
    assert res.workflow is not None
    assert res.workflow.id == proj_pick.id
    assert res.workflow.id != sys_default.id
    assert res.source == "project_default"


def test_project_default_cannot_bypass_capability_check(db):
    """项目默认配了不支持音频的工作流：必须报错，不得静默降级到别的。"""
    t2v = _add_workflow(db, "wf_video_h3_t2v", workflow_type="video")
    _add_workflow(db, "wf_video_h3_ref2va_std_5s", workflow_type="video")
    project = _add_project(db, {"default_workflows": {"video": t2v.id}})
    with pytest.raises(Exception) as exc:
        matcher.resolve(
            db, slot="video", project_id=project.id,
            extra_required={"modalities.audio_input": True},
        )
    detail = exc.value.detail  # type: ignore[attr-defined]
    assert detail["code"] == "workflow_capability_mismatch"
    assert detail["workflow_id"] == t2v.id
    assert "项目默认工作流" in detail["message"]


def test_project_default_missing_workflow_raises(db):
    project = _add_project(db, {"default_workflows": {"video": "wf_ghost"}})
    with pytest.raises(Exception) as exc:
        matcher.resolve(db, slot="video", project_id=project.id)
    assert exc.value.detail["code"] == "workflow_not_found"  # type: ignore[attr-defined]


def test_project_without_settings_falls_through_to_capability_match(db):
    _add_workflow(db, "wf_image_ref_qwen21_multi", workflow_type="image")
    project = _add_project(db, {})
    res = matcher.resolve(db, slot="image", project_id=project.id)
    assert res.workflow is not None
    assert res.source in ("capability_match", "system_default")


def test_save_project_defaults_validates_slot_and_type(db):
    wf = _add_workflow(db, "wf_image_t2i_qwen21_turbo", workflow_type="image")
    project = _add_project(db)
    # 槽位类型不匹配 → 422
    with pytest.raises(Exception) as exc:
        matcher.save_project_defaults(db, project.id, {"video": wf.id})
    assert exc.value.detail["code"] == "invalid_request"  # type: ignore[attr-defined]
    # 未知槽位 → 422
    with pytest.raises(Exception) as exc2:
        matcher.save_project_defaults(db, project.id, {"nonsense": wf.id})
    assert exc2.value.detail["code"] == "invalid_request"  # type: ignore[attr-defined]
    # 正常保存
    saved = matcher.save_project_defaults(db, project.id, {"image": wf.id, "storyboard_image": wf.id})
    assert saved == {"image": wf.id, "storyboard_image": wf.id}
    assert matcher.project_defaults(db, project.id)["image"] == wf.id


def test_save_project_defaults_drops_empty_values(db):
    """空值表示「恢复系统默认」，不应写入映射。"""
    wf = _add_workflow(db, "wf_image_t2i_qwen21_turbo", workflow_type="image")
    project = _add_project(db, {"default_workflows": {"image": wf.id}})
    saved = matcher.save_project_defaults(db, project.id, {"image": ""})
    assert saved == {}
    assert matcher.project_defaults(db, project.id) == {}


def test_unknown_slot_rejected(db):
    with pytest.raises(Exception) as exc:
        matcher.resolve(db, slot="hologram")
    assert exc.value.detail["code"] == "invalid_request"  # type: ignore[attr-defined]
