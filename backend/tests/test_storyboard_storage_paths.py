"""分镜图/分镜视频产物落盘路径回归：必须按项目分目录。

历史缺陷：``storyboard_{项目内全局镜号}_img`` 平铺写在 ``media/storyboards/`` 下，
两个项目都写 ``storyboard_1_img.png``，后跑的项目静默覆盖前一个项目的分镜图。
实测线上数据里 ``游乐场 / 末班地铁 / 参考图1125`` 三个项目的 storyboard_1_img
资产行同时指向同一个文件。

这些用例直接调用 executor 的落库函数，断言不同项目产出的文件落在各自目录、
互不覆盖。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Asset, Base


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """独立 sqlite + 独立媒体目录，避免污染真实 data/media。"""
    db_file = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod

    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod,
        "SessionLocal",
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )

    media_dir = tmp_path / "media"
    media_dir.mkdir()

    import app.worker.executor as executor

    settings = executor.get_settings()
    monkeypatch.setattr(settings, "media_dir", media_dir, raising=False)

    client = TestClient(app)
    return SimpleNamespace(client=client, media_dir=media_dir, executor=executor, engine=engine)


from app.main import app  # noqa: E402  (需在 monkeypatch fixture 之前导入无副作用)


def _with_db(fn):
    from app.core.database import SessionLocal

    db = SessionLocal()
    try:
        return fn(db)
    finally:
        db.close()


def _project(client: TestClient, name: str) -> str:
    return client.post("/api/projects", json={"name": name, "aspect_ratio": "9:16"}).json()["id"]


def _scene(client: TestClient, pid: str, title: str) -> dict:
    return client.post(
        "/api/scenes", json={"project_id": pid, "title": title, "episode": 1, "seq": 1}
    ).json()


def _shot(client: TestClient, scene_id: str) -> str:
    return client.post(
        "/api/shots",
        json={"scene_id": scene_id, "episode": 1, "shot_kind": "visual", "duration": 5},
    ).json()["id"]


def _fake_job(project_id: str, shot_id: str, idx: int) -> SimpleNamespace:
    """落库函数只读 job.id / project_id / shot_id / input_snapshot。

    注意：资产复用查询要 join jobs 表取 Job.shot_id，所以必须先落一行真 Job，
    否则「同镜重新生成」会被误判成「无历史资产」而新建行（测试用例曾因此假失败）。
    """
    from datetime import datetime, timezone

    from app.core.database import SessionLocal
    from app.models import Job

    job_id = f"job_test_{idx}"
    db = SessionLocal()
    try:
        db.add(
            Job(
                id=job_id,
                project_id=project_id,
                shot_id=shot_id,
                job_type="generate_keyframe",
                status="succeeded",
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        )
        db.commit()
    finally:
        db.close()
    return SimpleNamespace(
        id=job_id,
        project_id=project_id,
        shot_id=shot_id,
        input_snapshot=None,
    )


def _store(env, project_id: str, shot_id: str, content: bytes, ext: str = "png", idx: int = 1):
    def _run(db):
        from app.models import Shot

        shot = db.query(Shot).filter(Shot.id == shot_id).one()
        out = env.executor._store_shot_keyframe(
            db, _fake_job(project_id, shot_id, idx), shot, content, ext
        )
        db.commit()  # 落盘函数只 flush，用例需显式提交才能被下一次调用看到
        return out

    return _with_db(_run)


def _store_clip(env, project_id: str, shot_id: str, content: bytes, idx: int = 1):
    def _run(db):
        from app.models import Shot

        shot = db.query(Shot).filter(Shot.id == shot_id).one()
        out = env.executor._store_shot_clip(
            db, _fake_job(project_id, shot_id, idx), shot, content, "mp4"
        )
        db.commit()
        return out

    return _with_db(_run)


def test_keyframes_of_two_projects_do_not_share_file(env):
    """两个项目的镜号 1 必须落在各自项目目录，且内容互不覆盖。"""
    p1 = _project(env.client, "项目甲")
    p2 = _project(env.client, "项目乙")
    shot1 = _shot(env.client, _scene(env.client, p1, "便利店")["id"])
    shot2 = _shot(env.client, _scene(env.client, p2, "教室")["id"])

    r1 = _store(env, p1, shot1, b"AAA-FIRST-PROJECT", idx=1)
    r2 = _store(env, p2, shot2, b"BBB-SECOND-PROJECT", idx=2)

    assert r1["file_path"] != r2["file_path"], "两个项目的分镜图不能是同一个文件"
    assert os.path.basename(r1["file_path"]) == os.path.basename(r2["file_path"]) == "storyboard_1_img.png"
    assert p1 in r1["file_path"] and p2 in r2["file_path"], "路径必须包含各自项目 id"

    # 关键断言：先后生成后，先前的文件内容必须还在（没被覆盖）
    with open(r1["file_path"], "rb") as f:
        assert f.read() == b"AAA-FIRST-PROJECT"
    with open(r2["file_path"], "rb") as f:
        assert f.read() == b"BBB-SECOND-PROJECT"


def test_keyframe_regenerate_overwrites_in_place_same_asset(env):
    """同项目同镜重新生成：复用同一资产行，文件原地覆盖，不产生孤儿行。"""
    pid = _project(env.client, "项目丙")
    shot_id = _shot(env.client, _scene(env.client, pid, "雨夜街道")["id"])

    first = _store(env, pid, shot_id, b"V1", idx=1)
    second = _store(env, pid, shot_id, b"V2", idx=2)

    assert first["asset_id"] == second["asset_id"]
    with open(second["file_path"], "rb") as f:
        assert f.read() == b"V2"

    def _count(db):
        return db.query(Asset).filter(
            Asset.project_id == pid, Asset.asset_type == "keyframe"
        ).count()

    assert _with_db(_count) == 1


def test_clip_paths_also_per_project(env):
    """分镜视频同口径：两个项目的镜号 1 视频不能共用同一文件。"""
    p1 = _project(env.client, "项目丁")
    p2 = _project(env.client, "项目戊")
    s1 = _shot(env.client, _scene(env.client, p1, "天台")["id"])
    s2 = _shot(env.client, _scene(env.client, p2, "走廊")["id"])

    r1 = _store_clip(env, p1, s1, b"CLIP-A", idx=1)
    r2 = _store_clip(env, p2, s2, b"CLIP-B", idx=2)

    assert r1["file_path"] != r2["file_path"], "两个项目的分镜视频不能是同一个文件"
    with open(r1["file_path"], "rb") as f:
        assert f.read() == b"CLIP-A"
    with open(r2["file_path"], "rb") as f:
        assert f.read() == b"CLIP-B"
