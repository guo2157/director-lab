"""分镜图角色参考槽位：多角色镜头必须绑定多个 ref_character_N。

缺陷背景（2026-11-21）：
`build_asset_plans` 原先只读 `shot.character_id` 绑定单个角色，
于是「提示词里 @ 了两个人、参考图只给一个槽位」成为系统性现象。
实测 prj_257406918e63：26 个镜头里 8 个提示词 @ 了两个角色，
但 AssetPlanDependency 全部只有 ref_identity + ref_scene 两条，
第二个人物从头到尾没有参考图，生成出的分镜图长相无法保证。

同一份数据的实时解析器 `resolve_shot_references` 却能正确算出
ref_identity + ref_character_2 —— 两条链路口径不一致。

修复：依赖绑定改用 shot_refs.shot_subject_characters（权威口径），
主角色 → ref_identity，其余按出现顺序 → ref_character_2/3/...

本测试锁死该行为，防止回归。
"""
import pytest

from app.core.database import make_engine
from app.models import Base, Character, Project, Scene, Shot
from app.services.asset_plan import build_asset_plans
from app.models import AssetPlan, AssetPlanDependency


@pytest.fixture()
def session(tmp_path):
    from sqlalchemy.orm import sessionmaker

    engine = make_engine(f"sqlite:///{tmp_path / 't.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = Session()
    yield s
    s.close()


def _mk_project(session, name="多角色验证"):
    p = Project(id="prj_multirole", name=name, aspect_ratio="9:16",
                creation_type="script", status="active")
    session.add(p)
    session.flush()
    return p


def _mk_chars(session, project, names):
    out = {}
    for i, n in enumerate(names):
        c = Character(id=f"chr_{i:04d}", project_id=project.id, name=n,
                      description=f"{n}的描述", appearance=f"{n}的外貌")
        session.add(c)
        out[n] = c
    session.flush()
    return out


def _mk_scene(session, project, title="教室"):
    sc = Scene(id="scn_0001", project_id=project.id, title=title,
               description="午后教室", seq=1, episode=1)
    session.add(sc)
    session.flush()
    return sc


def _deps_of(session, shot_id, plan_type="keyframe"):
    """返回 {slot: 被引用实体的 owner_id}，便于按角色 id 精确断言。

    ref_key 是 `chr_4853377a` 这类哈希，与 Character.id 无关，
    断言必须顺着 ref_plan_id → AssetPlan.owner_id 反查。
    """
    plan = (
        session.query(AssetPlan)
        .filter(AssetPlan.owner_id == shot_id, AssetPlan.plan_type == plan_type)
        .first()
    )
    if plan is None:
        return {}
    rows = (
        session.query(AssetPlanDependency)
        .filter(AssetPlanDependency.plan_id == plan.id)
        .all()
    )
    out = {}
    for d in rows:
        owner = None
        if d.ref_plan_id:
            ref_plan = session.get(AssetPlan, d.ref_plan_id)
            owner = ref_plan.owner_id if ref_plan else None
        out[d.slot] = owner
    return out


def test_two_characters_in_prompt_get_two_slots(session):
    """提示词 @ 两个角色 → ref_identity + ref_character_2 都要绑上。"""
    p = _mk_project(session)
    chars = _mk_chars(session, p, ["苏晚", "陆屿"])
    sc = _mk_scene(session, p)
    shot = Shot(
        id="sht_0001", scene_id=sc.id, episode=1, seq=1,
        character_id=chars["苏晚"].id,
        image_prompt="中景。@苏晚 站在左侧，@陆屿 站在右侧，两人对视。",
        shot_kind="visual", duration=5,
    )
    session.add(shot)
    session.flush()

    build_asset_plans(session, p)
    session.flush()

    deps = _deps_of(session, shot.id)
    assert "ref_identity" in deps, f"缺主角色槽位，实际 {list(deps)}"
    assert "ref_character_2" in deps, (
        f"提示词 @ 了两个角色但只绑了一个槽位，实际 {list(deps)}"
    )
    # 两个槽位必须指向不同角色
    assert deps["ref_identity"] != deps["ref_character_2"], (
        f"两个角色槽位指向同一实体 {deps}"
    )
    assert {deps["ref_identity"], deps["ref_character_2"]} == {
        chars["苏晚"].id, chars["陆屿"].id
    }, f"槽位未覆盖全部两个角色，实际 {deps}"


def test_primary_character_goes_to_ref_identity(session):
    """character_id 指定的角色占 ref_identity，另一个才占 ref_character_2。"""
    p = _mk_project(session, "主角色顺序")
    chars = _mk_chars(session, p, ["苏晚", "陆屿"])
    sc = _mk_scene(session, p)
    shot = Shot(
        id="sht_0002", scene_id=sc.id, episode=1, seq=1,
        character_id=chars["陆屿"].id,
        image_prompt="@苏晚 在前景虚化，@陆屿 在焦点上。",
        shot_kind="visual", duration=5,
    )
    session.add(shot)
    session.flush()

    build_asset_plans(session, p)
    session.flush()

    deps = _deps_of(session, shot.id)
    assert deps.get("ref_identity") == chars["陆屿"].id, (
        f"character_id=陆屿 却未占 ref_identity，实际 {deps}"
    )
    assert deps.get("ref_character_2") == chars["苏晚"].id, (
        f"次要角色苏晚未占 ref_character_2，实际 {deps}"
    )


def test_dialogue_speaker_counts_as_subject(session):
    """只有对白说话人、没有 character_id 标注的角色也要拿到槽位。"""
    p = _mk_project(session, "对白说话人")
    chars = _mk_chars(session, p, ["苏晚", "陆屿"])
    sc = _mk_scene(session, p)
    shot = Shot(
        id="sht_0003", scene_id=sc.id, episode=1, seq=1,
        character_id=chars["苏晚"].id,
        dialogue="你先说。",
        dialogue_segments=[
            {"speaker": "陆屿", "speaker_id": chars["陆屿"].id,
             "text": "那我先说。", "order": 1},
        ],
        image_prompt="近景，@苏晚 侧脸。",
        shot_kind="dialogue", duration=5,
    )
    session.add(shot)
    session.flush()

    build_asset_plans(session, p)
    session.flush()

    deps = _deps_of(session, shot.id)
    assert chars["陆屿"].id in deps.values(), (
        f"对白说话人陆屿未获得参考槽位，实际 {deps}"
    )


def test_single_character_gets_exactly_one_slot(session):
    """单角色镜头不应重复占两个槽位（历史上有过 ref_identity+ref_character_2 同指）。"""
    p = _mk_project(session, "单角色")
    chars = _mk_chars(session, p, ["苏晚"])
    sc = _mk_scene(session, p)
    shot = Shot(
        id="sht_0004", scene_id=sc.id, episode=1, seq=1,
        character_id=chars["苏晚"].id,
        image_prompt="近景，@苏晚 站在窗前。",
        shot_kind="visual", duration=5,
    )
    session.add(shot)
    session.flush()

    build_asset_plans(session, p)
    session.flush()

    deps = _deps_of(session, shot.id)
    char_slots = [s for s in deps if s == "ref_identity" or s.startswith("ref_character_")]
    assert len(char_slots) == 1, (
        f"单角色镜头占了 {len(char_slots)} 个角色槽位：{char_slots}"
    )
    assert char_slots[0] == "ref_identity"
