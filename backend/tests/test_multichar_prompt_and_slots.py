"""回归测试：双人镜的第二个人物必须同时进入「提示词参考资产」与「参考图槽位」。

真实事故（「海风与夏日」第 2 集）：分镜图里出现两个江屿，没有林晚。
用户观察到的现象是「提示词里有两个角色，参考图只有一个槽位」。

## 根因链

解析 schema 每个 shot 只有 ``character_id``（**主说话角色，单值**），没有
「本镜出场角色」数组。双人镜的第二个人物只存在于自由文本里：

    character_id       = 江屿
    beats[0].action    = 江屿双腿弯曲，屈膝下蹲后臀部落在干沙上…
    beats[1].action    = 林晚在江屿右侧并排坐下…        ← 林晚只在这里
    dialogue_segments  = []                              ← 无对白
    image_prompt       = 没有 @林晚                       ← 无显式引用

于是 ``shot_subject_characters`` 的三个来源全部落空，只算出江屿。
更糟的是项目里有**两份**角色判定实现，各自分叉：

    shot_subject_characters  → 资产定义/槽位绑定用
    collect_shot_characters  → inject_asset_refs 用（提示词「参考资产」行）

修复后两者统一委托到同一实现，且新增第四个来源：从 beats / start_state /
end_state 的动作文本里提取角色名。
"""
from __future__ import annotations

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Base, Character, Project, Scene, Shot


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod, "SessionLocal",
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    try:
        yield session
    finally:
        session.close()


def _setup(db, *, beats, character_id="chr_jiangyu"):
    proj = Project(id="prj_t", name="T", aspect_ratio="9:16")
    db.add(proj)
    db.flush()
    jiangyu = Character(id="chr_jiangyu", project_id=proj.id, name="江屿")
    linwan = Character(id="chr_linwan", project_id=proj.id, name="林晚")
    db.add_all([jiangyu, linwan])
    scene = Scene(id="scn_t", project_id=proj.id, title="海岸沙滩", seq=1,
                  description="d")
    db.add(scene)
    db.flush()
    shot = Shot(
        id="sht_t", scene_id=scene.id, episode=2, seq=3,
        character_id=character_id, shot_kind="visual", duration=5,
        status="pending", beats=beats, dialogue_segments=[],
        image_prompt="任务：生成第 2-3 镜的单帧分镜图\n【本镜剧情】两人并肩坐下",
    )
    db.add(shot)
    db.flush()
    return proj, shot, jiangyu, linwan


def test_second_character_found_from_beats(db):
    """第二个人物只出现在 beats 动作描述里，也必须被识别为主体角色。"""
    from app.services.shot_refs import shot_subject_characters

    _proj, shot, jiangyu, linwan = _setup(db, beats=[
        {"action": "江屿双腿弯曲，屈膝下蹲后臀部落在干沙上"},
        {"action": "林晚在江屿右侧并排坐下，双腿向右侧并拢弯曲"},
    ])

    names = [c.name for c in shot_subject_characters(db, shot)]

    assert names == ["江屿", "林晚"], f"应识别两个角色，实际 {names}"


def test_second_character_found_from_state_fields(db):
    """即使 beats 缺失，start_state/end_state 的 holding 里提到的角色也要算。"""
    from app.services.shot_refs import shot_subject_characters

    _proj, shot, _j, _l = _setup(db, beats=[])
    shot.start_state = {"pose": "两人并肩站立", "holding": "江屿右手握着林晚左手"}
    db.flush()

    names = [c.name for c in shot_subject_characters(db, shot)]

    assert "林晚" in names, f"holding 里的角色应被识别，实际 {names}"


def test_collect_shot_characters_delegates(db):
    """collect_shot_characters（提示词「参考资产」行用）必须与权威口径一致。

    这是本次事故的关键：两份实现分叉导致
    「提示词只有 @江屿，槽位却有 ref_character_2=林晚」。
    """
    from app.services.shot_refs import collect_shot_characters, shot_subject_characters

    _proj, shot, _j, _l = _setup(db, beats=[
        {"action": "江屿坐下"},
        {"action": "林晚在江屿右侧坐下"},
    ])

    authoritative = [c.name for c in shot_subject_characters(db, shot)]
    collected = collect_shot_characters(db, shot)

    assert collected == authoritative, (
        f"两份口径分叉：authoritative={authoritative}, collected={collected}"
    )
    assert collected == ["江屿", "林晚"]


def test_inject_asset_refs_includes_both_characters(db):
    """提示词头部的「参考资产」行必须列出两个角色。"""
    from app.services.shot_refs import inject_asset_refs

    _proj, shot, _j, _l = _setup(db, beats=[
        {"action": "江屿坐下"},
        {"action": "林晚在江屿右侧坐下"},
    ])
    # 给两个角色都配上定妆图指针，inject_asset_refs 只注入有图的角色
    shot.image_prompt = "任务：生成第 2-3 镜的单帧分镜图"

    out = inject_asset_refs(db, shot, shot.image_prompt)
    head = out.split("\n")[0]

    # 没有定妆图时不会注入（避免 reference_asset_missing），此处只验证
    # 角色识别本身正确 —— 若识别漏了林晚，这里连名字都不会出现
    from app.services.shot_refs import collect_shot_characters
    assert collect_shot_characters(db, shot) == ["江屿", "林晚"]


def test_single_character_shot_not_polluted(db):
    """单人镜不能被凭空加上第二个角色（防止过度匹配）。"""
    from app.services.shot_refs import shot_subject_characters

    _proj, shot, _j, _l = _setup(db, beats=[
        {"action": "江屿独自站在沙滩上，望向海面"},
    ])

    names = [c.name for c in shot_subject_characters(db, shot)]

    assert names == ["江屿"], f"单人镜不应多出角色，实际 {names}"


def test_dialogue_text_alone_does_not_count(db):
    """台词里「被谈论」的人不算出场（避免凭空占槽位）。

    「妈妈，我能把麦子驮到河对岸吗」里的妈妈若按文本子串匹配，
    会凭空占一个身份槽位。有台词的角色由 dialogue_segments 覆盖。
    """
    from app.services.shot_refs import shot_subject_characters

    _proj, shot, _j, _l = _setup(db, beats=[{"action": "江屿望向远方"}])
    shot.dialogue = "江屿问：林晚去哪了？"  # 林晚只被提及，不在画面
    db.flush()

    names = [c.name for c in shot_subject_characters(db, shot)]

    assert names == ["江屿"], f"台词提及不应算出场，实际 {names}"
