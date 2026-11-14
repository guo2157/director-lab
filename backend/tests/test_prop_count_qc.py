"""道具数量 / 持有关系确定性校验与参考图槽位硬校验。

覆盖：
- holding 数量 → beats 交接 → 目标数量三者一致时放行；
- 递出一杯但目标状态仍写两杯时阻断（PROP_COUNT_CONFLICT）；
- 没有任何交接事件却自行增减数量时阻断；
- 上一镜末态与本镜开场数量不一致时给出 warning；
- 身份槽注入非主体角色定妆图、时序槽注入非尾帧资产时编译期报错。
"""
import pytest

from app.core.database import make_engine
from app.models import Asset, Base, Character, Project, Prop, Scene, Shot, ids


@pytest.fixture()
def db(tmp_path):
    from sqlalchemy.orm import sessionmaker

    engine = make_engine(f"sqlite:///{tmp_path / 'qc.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()


def _file(tmp_path, name: str) -> str:
    path = tmp_path / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
    return str(path)


def _project(db) -> Project:
    p = Project(id=ids.project_id(), name="质检", creation_type="novel",
                aspect_ratio="9:16", status="active", assets_review_status="approved")
    db.add(p)
    db.commit()
    return p


def _character(db, project, tmp_path, name: str) -> Character:
    asset = Asset(id=ids.asset_id(), project_id=project.id, asset_type="character",
                  name=f"{name}-定妆", version=1, validity="current",
                  file_path=_file(tmp_path, f"{name}.png"))
    db.add(asset)
    db.flush()
    ch = Character(id=ids.character_id(), project_id=project.id, name=name,
                   appearance="测试外观", portrait_asset_id=asset.id)
    db.add(ch)
    db.commit()
    return ch


def _scene(db, project) -> Scene:
    sc = Scene(id=ids.scene_id(), project_id=project.id, episode=1, seq=1,
               title="饮料店", description="柜台与高脚凳")
    db.add(sc)
    db.commit()
    return sc


def _shot(db, scene, character, *, seq, start=None, end=None, beats=None,
          last_frame=None) -> Shot:
    s = Shot(id=ids.shot_id(), scene_id=scene.id, episode=1, seq=seq,
             character_id=character.id, shot_kind="visual", duration=5,
             start_state=start, end_state=end, beats=beats,
             last_frame_asset_id=last_frame, keyframe_confirmed=False)
    db.add(s)
    db.commit()
    return s


def test_transfer_quantity_consistent_passes(db, tmp_path):
    from app.services.prompt_qc import check_prop_count_consistency

    project = _project(db)
    su = _character(db, project, tmp_path, "苏晚")
    lu = _character(db, project, tmp_path, "陆屿")
    db.add(Prop(id=ids.prop_id(), project_id=project.id, name="冰可乐"))
    scene = _scene(db, project)
    shot = _shot(
        db, scene, su, seq=3,
        start={"holding": "苏晚：左手一杯冰可乐，右手一杯冰可乐；陆屿：空手"},
        end={"holding": "苏晚：左手仍持一杯冰可乐，右手空着；陆屿：左手持一杯冰可乐"},
        beats=[{"index": 1, "action": "苏晚右手将一杯冰可乐递给陆屿左手"}],
    )
    assert check_prop_count_consistency(db, shot) == []


def test_transfer_but_target_state_keeps_two_cups_blocks(db, tmp_path):
    from app.services.prompt_qc import check_prop_count_consistency

    project = _project(db)
    su = _character(db, project, tmp_path, "苏晚")
    _character(db, project, tmp_path, "陆屿")
    db.add(Prop(id=ids.prop_id(), project_id=project.id, name="冰可乐"))
    scene = _scene(db, project)
    shot = _shot(
        db, scene, su, seq=3,
        start={"holding": "苏晚：左手一杯冰可乐，右手一杯冰可乐；陆屿：空手"},
        end={"holding": "苏晚：双手各持一杯冰可乐；陆屿：左手持一杯冰可乐"},
        beats=[{"index": 1, "action": "苏晚右手将一杯冰可乐递给陆屿左手"}],
    )
    issues = check_prop_count_consistency(db, shot)
    assert issues and issues[0]["code"] == "PROP_COUNT_CONFLICT"
    assert "数量冲突" in issues[0]["detail"]


def test_quantity_changes_without_event_blocks(db, tmp_path):
    from app.services.prompt_qc import check_prop_count_consistency

    project = _project(db)
    su = _character(db, project, tmp_path, "苏晚")
    db.add(Prop(id=ids.prop_id(), project_id=project.id, name="冰可乐"))
    scene = _scene(db, project)
    shot = _shot(
        db, scene, su, seq=3,
        start={"holding": "左手一杯冰可乐，右手一杯冰可乐"},
        end={"holding": "左手一杯冰可乐，右手一杯冰可乐，桌上还有一杯冰可乐"},
        beats=[{"index": 1, "action": "苏晚抬头看向门口"}],
    )
    issues = check_prop_count_consistency(db, shot)
    assert issues and issues[0]["code"] == "PROP_COUNT_CONFLICT"


def test_prev_state_mismatch_emits_warning(db, tmp_path):
    from app.services.prompt_qc import check_prev_state_continuity

    project = _project(db)
    su = _character(db, project, tmp_path, "苏晚")
    db.add(Prop(id=ids.prop_id(), project_id=project.id, name="冰可乐"))
    scene = _scene(db, project)
    last = Asset(id=ids.asset_id(), project_id=project.id, asset_type="last_frame",
                 name="第2镜尾帧", version=1, validity="current",
                 file_path=_file(tmp_path, "last.png"))
    db.add(last)
    db.flush()
    _shot(db, scene, su, seq=2,
          end={"holding": "左手持一杯冰可乐"},
          last_frame=last.id)
    current = _shot(db, scene, su, seq=3,
                    start={"holding": "左手一杯冰可乐，右手一杯冰可乐"})
    warnings = check_prev_state_continuity(db, current)
    assert warnings and "冰可乐" in warnings[0]


def test_identity_slot_rejects_non_subject_character(db, tmp_path):
    from app.services.prompt_compiler import audit_reference_slots

    project = _project(db)
    su = _character(db, project, tmp_path, "苏晚")
    lu = _character(db, project, tmp_path, "陆屿")
    scene = _scene(db, project)
    shot = _shot(db, scene, su, seq=3)
    lu_portrait = db.get(Asset, lu.portrait_asset_id)
    issues = audit_reference_slots(db, shot, {"ref_identity": lu_portrait.file_path})
    assert issues and issues[0]["code"] == "identity_reference_leak"


def test_duplicate_reference_binding_is_rejected(db, tmp_path):
    from app.services.prompt_compiler import audit_reference_slots

    project = _project(db)
    su = _character(db, project, tmp_path, "苏晚")
    scene = _scene(db, project)
    shot = _shot(db, scene, su, seq=3)
    portrait = db.get(Asset, su.portrait_asset_id)
    issues = audit_reference_slots(
        db, shot,
        {"ref_identity": portrait.file_path, "ref_character_2": portrait.file_path},
    )
    assert any(i["code"] == "reference_slot_duplicate" for i in issues)


def test_structure_check_flags_truncated_and_empty_heading():
    from app.services.prompt_qc import check_prompt_structure

    prompt = "【六、道具连续性】\n杯子保持一只\n\n【七、\n\n【八、禁止】\n换脸"
    warnings = check_prompt_structure(prompt)
    assert any("【七" in item for item in warnings)


def test_prev_frame_slot_rejects_non_last_frame_asset(db, tmp_path):
    from app.services.prompt_compiler import audit_reference_slots

    project = _project(db)
    su = _character(db, project, tmp_path, "苏晚")
    scene = _scene(db, project)
    keyframe = Asset(id=ids.asset_id(), project_id=project.id, asset_type="keyframe",
                     name="分镜图", version=1, validity="current",
                     file_path=_file(tmp_path, "keyframe.png"))
    db.add(keyframe)
    db.commit()
    shot = _shot(db, scene, su, seq=3)
    issues = audit_reference_slots(db, shot, {"ref_prev_frame": keyframe.file_path})
    assert issues and issues[0]["code"] == "reference_asset_type_mismatch"
