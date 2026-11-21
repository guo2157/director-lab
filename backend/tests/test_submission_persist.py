"""回归测试：提交生成前必须把「本次下发的提示词 + 参考图槽位」回写为详情页数据源。

用户报障原话（2026-11-21）：

> 从第二镜起待生成的视频状态如果从待生成转化为视频生成中，这视频生成中之前有
> 调整提示词和参考图槽位的操作，然后提交生成视频，这生成视频前要先保存修改后的
> 提示词和参考图，这样用户查看分镜视频详情的时候就是看到对应生成的提示词和参考图，
> 槽位也能对应上。

## 根因：两条提交链路的回写行为不一致

| 入口 | 回写 shot.video_prompt | 保存 StoryboardRegenConfig |
|---|---|---|
| `POST /shots/{id}/regenerate/workspace/regenerate`（重新生成）| ✅ | ✅ |
| `POST /shots/{id}/generate`（首次生成，待生成 → 生成中）| ❌ | ❌ |

详情页 `useShotDetail.open()` 读 `shot.video_prompt`，
`loadRefBindings()` 读 `StoryboardRegenConfig`。首次生成两个都没写，
页面只能显示解析阶段原始文本 + 实时推导槽位，与本次实际下发的内容不同。

真实数据（修复前）：
```
job_05bd73d83fe0  sht_455fd0a48eec  status=running  saved_cfg=0
  job.prompt        tags=['1','2','3']  len=1224
  shot.video_prompt tags=['1']          len=878
```
"""
from __future__ import annotations

import re

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Asset, Base, Character, Job, Project, Scene, Shot
from app.models.regen_config import StoryboardRegenConfig
from app.services.submission_persist import build_references, persist_submission

DECLARED = [
    {"param": "ref_prev_frame", "input_key": "ref_images.ref_image_0"},
    {"param": "ref_image", "input_key": "ref_images.ref_image_1"},
    {"param": "ref_identity", "input_key": "ref_images.ref_image_2"},
    {"param": "ref_character_2", "input_key": "ref_images.ref_image_3"},
]


@pytest.fixture()
def db(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 't.db'}")
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


def _setup(db):
    proj = Project(id="prj_t", name="T", aspect_ratio="9:16")
    db.add(proj)
    db.flush()
    lin = Character(id="chr_lin", project_id=proj.id, name="林晚")
    jiang = Character(id="chr_jiang", project_id=proj.id, name="江屿")
    db.add_all([lin, jiang])
    scene = Scene(id="scn_t", project_id=proj.id, title="海岸沙滩", seq=1, description="d")
    db.add(scene)
    db.flush()
    shot = Shot(id="sht_t", scene_id=scene.id, episode=2, seq=2, duration=5,
                shot_kind="visual")
    db.add(shot)
    db.flush()
    # 真实文件路径必须存在，否则资产名解析会被跳过
    import pathlib

    tmpdir = pathlib.Path(db.bind.url.database).parent
    files = {}
    for aid, name, atype in [
        ("ast_kf", "storyboard_16_img", "keyframe"),
        ("ast_lin", "林晚", "character"),
        ("ast_jiang", "江屿", "character"),
    ]:
        p = tmpdir / f"{aid}.png"
        p.write_bytes(b"x")
        db.add(Asset(
            id=aid, project_id=proj.id, asset_type=atype, name=name,
            file_path=str(p), validity="current", version=1,
        ))
        files[aid] = str(p)
    db.flush()
    return proj, scene, shot, files


def test_persist_writes_prompt_and_references(db):
    """提交后：shot.video_prompt 与配置里的 references 都要按本次实际槽位落库。"""
    proj, scene, shot, files = _setup(db)
    prompt = "任务：Ref2VA 第 2-2 镜\n\n正文…"
    media = {
        "ref_image": files["ast_kf"],
        "ref_identity": files["ast_lin"],
        "ref_character_2": files["ast_jiang"],
    }
    summary = persist_submission(
        db, shot=shot, project_id=proj.id, target="clip",
        prompt=prompt, media=media, declared_slots=DECLARED,
        workflow_id="wf_t", workflow_version=3,
    )
    db.commit()

    assert summary["prompt_saved"] is True
    assert summary["references"] == 3
    # 编号必须是「实际注入槽位压缩后」的连续序号：无尾帧 → 1/2/3
    assert summary["slot_numbers"] == {
        "ref_image": 1, "ref_identity": 2, "ref_character_2": 3,
    }

    shot2 = db.get(Shot, shot.id)
    assert shot2.video_prompt == prompt, "详情页 open() 读的就是这个字段"

    row = (
        db.query(StoryboardRegenConfig)
        .filter(StoryboardRegenConfig.shot_id == shot.id)
        .filter(StoryboardRegenConfig.target == "clip")
        .one()
    )
    assert row.final_prompt == prompt
    slots = {r["slot_id"]: r["image_no"] for r in row.references}
    assert slots == {"ref_image": 1, "ref_identity": 2, "ref_character_2": 3}
    # 资产名要能解析出来（否则页面显示成槽位名，职责块退化成 @角色）
    names = {r["slot_id"]: r["asset_name"] for r in row.references}
    assert names["ref_identity"] == "林晚"
    assert names["ref_character_2"] == "江屿"


def test_persist_is_idempotent_and_bumps_version(db):
    """重复提交只递增版本，不产生第二份配置（唯一约束 + upsert）。"""
    proj, scene, shot, files = _setup(db)
    media = {"ref_image": files["ast_kf"]}
    for _ in range(2):
        persist_submission(
            db, shot=shot, project_id=proj.id, target="clip",
            prompt="p", media=media, declared_slots=DECLARED,
        )
        db.commit()
    rows = db.query(StoryboardRegenConfig).filter(
        StoryboardRegenConfig.shot_id == shot.id
    ).all()
    assert len(rows) == 1
    assert rows[0].version == 2


def test_persist_never_blocks_generation_on_error(db, monkeypatch):
    """回写是展示层一致性，任何异常都不能让一次能跑的生成失败。"""
    proj, scene, shot, files = _setup(db)

    def _boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr("app.services.submission_persist.build_references", _boom)
    summary = persist_submission(
        db, shot=shot, project_id=proj.id, target="clip",
        prompt="p", media={"ref_image": files["ast_kf"]}, declared_slots=DECLARED,
    )
    assert summary["error"] == "persist_failed"


def test_references_numbers_follow_compressed_not_static(db):
    """槽位编号必须是压缩后的连续序号，不能照搬工作流静态 image_N。

    静态声明里 ref_image 是第 2 张；没有尾帧时它实际是模型收到的第 1 张。
    页面若显示静态编号，就会与提示词里的 <Picture 1> 对不上。
    """
    proj, scene, shot, files = _setup(db)
    refs = build_references(
        db, project_id=proj.id,
        media={"ref_image": files["ast_kf"], "ref_identity": files["ast_lin"]},
        declared_slots=DECLARED,
    )
    assert [r["image_no"] for r in refs] == [1, 2]
    # 静态编号留痕，排查时能一眼看出「压缩过」
    assert refs[0]["static_image_no"] == 2
    assert refs[1]["static_image_no"] == 3


def test_windows_style_paths_still_resolve_names(db):
    """历史数据里路径 `/` 与 `\\` 混用，等值查询会匹配不到 → 名字退化成槽位名。"""
    proj, scene, shot, files = _setup(db)
    win_path = files["ast_lin"].replace("/", "\\")
    refs = build_references(
        db, project_id=proj.id,
        media={"ref_identity": win_path},
        declared_slots=DECLARED,
    )
    assert refs[0]["asset_name"] == "林晚"


def test_references_survive_payload_without_image_url(db):
    """前端只传 {slot_id, asset_id}（无 image_url）时，编号仍必须算得出来。

    真实 e2e 阶段 C 踩到：保存的 references 直接用了前端 payload，
    image_url 全空 → compressed_numbers 的 active 集合为空 → image_no 全 None
    → 详情页提示词 tags 退回 ['1']、槽位与提示词对不上。
    """
    proj, scene, shot, files = _setup(db)
    # 模拟前端 payload 形态：只有 slot_id + asset_id
    frontend_payload = [
        {"slot_id": "ref_image", "asset_id": "ast_kf", "enabled": True},
        {"slot_id": "ref_identity", "asset_id": "ast_lin", "enabled": True},
    ]
    # 后端应当忽略它、改用实际注入的 __media__ 重建
    media = {"ref_image": files["ast_kf"], "ref_identity": files["ast_lin"]}
    refs = build_references(
        db, project_id=proj.id, media=media, declared_slots=DECLARED
    )
    assert [r["image_no"] for r in refs] == [1, 2]
    assert all(r["image_url"] for r in refs), "image_url 必须落库，否则详情页拿不到图"
    assert all(r["asset_id"] for r in refs)


def test_saved_config_numbers_recomputed_when_image_url_missing(db):
    """saved 配置里 image_url 为空但 asset_id 在 → 编号仍要算出来（页面不该显示 None）。"""
    from app.services import reference_layout as rl

    # 模拟 regen_workspace.build_workspace 里的 active 判定
    references = [
        {"slot_id": "ref_image", "asset_id": "ast_kf", "image_url": None, "enabled": True},
        {"slot_id": "ref_identity", "asset_id": "ast_lin", "image_url": None, "enabled": True},
    ]
    active = [
        r["slot_id"] for r in references
        if r.get("enabled", True) and r.get("slot_id")
        and (r.get("image_url") or r.get("asset_id"))
    ]
    numbers = rl.compressed_numbers(active, DECLARED)
    assert numbers == {"ref_image": 1, "ref_identity": 2}
