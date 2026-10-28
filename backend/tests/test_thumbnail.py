"""第四期测试（D-17 缩略图）：产物落库时生成缩略图并填充 thumb_path。

- 图片产物：Pillow 生成 400px webp 缩略图
- 视频产物：ffmpeg 抽取第 1 秒封面帧
- 音频产物：不生成缩略图（thumb_path 为 None）
- Mock 模式端到端：产物落库后 thumb_path 非空
"""
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.core.database import make_engine
from app.models import Base


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    monkeypatch.setattr(db_mod, "engine", engine)
    from sqlalchemy.orm import sessionmaker
    monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    return TestClient(app)


def test_make_thumb_image_and_video(tmp_path):
    """图片与视频缩略图生成；音频不生成。"""
    from PIL import Image

    from app.services.thumbnail import make_thumb

    # 图片
    img_path = tmp_path / "a_v1.png"
    Image.new("RGB", (1024, 576), color=(10, 20, 30)).save(img_path)
    thumb = make_thumb(str(img_path), "a_v1")
    assert thumb is not None
    assert thumb.endswith("a_v1_thumb.webp")
    with Image.open(thumb) as t:
        assert max(t.size) <= 400

    # 视频（ffmpeg testsrc 生成 2 秒样本）
    import subprocess
    vid_path = tmp_path / "b_v1.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc=size=320x180:rate=12:duration=2",
         "-pix_fmt", "yuv420p", "-c:v", "libx264", str(vid_path)],
        check=True, capture_output=True, timeout=30,
    )
    thumb = make_thumb(str(vid_path), "b_v1")
    assert thumb is not None and thumb.endswith("b_v1_thumb.webp")

    # 音频：不生成
    aud_path = tmp_path / "c_v1.wav"
    aud_path.write_bytes(b"RIFF" + b"\x00" * 100)
    assert make_thumb(str(aud_path), "c_v1") is None


def test_mock_job_generates_thumb(client: TestClient, monkeypatch, tmp_path):
    """Mock 模式端到端：产物落库后 thumb_path 非空（图片产物）。"""
    import json

    import app.worker.executor as ex_mod
    from app.core.config import Settings

    monkeypatch.setattr(ex_mod, "get_settings", lambda: Settings(render_mock=True, media_dir=tmp_path / "media"))

    pid = client.post("/api/projects", json={"name": "缩略图项目", "aspect_ratio": "9:16"}).json()["id"]
    # D-23 资产审核闸口：本用例聚焦缩略图生成，
    # 直接放行公共资产人工审核，避免与资产审核前置条件耦合
    from app.core.database import SessionLocal as _SL
    from app.models import Project as _Project
    _db0 = _SL()
    try:
        _p0 = _db0.get(_Project, pid)
        _p0.assets_review_status = "approved"
        _db0.commit()
    finally:
        _db0.close()
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "教室", "episode": 1}).json()
    shot = client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": 1, "shot_kind": "visual", "duration": 5,
    }).json()
    definition = {
        "1": {
            "class_type": "EmptyLatentImage",
            "_meta": {"title": "$~width, $~height"},
            "inputs": {"width": 512, "height": 288, "batch_size": 1},
        },
        "2": {
            "class_type": "SaveImage",
            "_meta": {"title": "$output.cover"},
            "inputs": {"images": ["1", 0]},
        },
    }
    wf = client.post(
        "/api/workflows",
        data={"name": "thumb-image", "workflow_type": "image"},
        files={"file": ("thumb_image.json", json.dumps(definition), "application/json")},
    ).json()
    j = client.post(f"/api/shots/{shot['id']}/generate", json={"target": "keyframe", "workflow_id": wf["id"]}).json()
    jid = j.get("id") or j.get("job", {}).get("id")
    from app.worker.executor import execute_generate_job
    result = execute_generate_job(jid)
    assert result["ok"] is True, result
    # 产物 thumb_path 非空
    from app.core.database import SessionLocal
    from app.models import Asset
    db = SessionLocal()
    a = db.query(Asset).filter(Asset.source_job_id == jid).first()
    db.close()
    assert a is not None
    assert a.thumb_path is not None and a.thumb_path.endswith("_thumb.webp")


def test_asset_file_endpoint(client: TestClient, monkeypatch, tmp_path):
    """媒体文件服务端点：原文件 200、thumb=true 返回缩略图、缩略图缺失回退原文件、文件不存在 404。"""
    from PIL import Image
    from app.core.database import SessionLocal
    from app.models import Asset, ids
    pid = client.post("/api/projects", json={"name": "文件服务项目", "aspect_ratio": "9:16"}).json()["id"]
    # 构造带缩略图的资产
    src = tmp_path / "a_v1.png"
    Image.new("RGB", (800, 600), color=(1, 2, 3)).save(src)
    thumb = tmp_path / "a_v1_thumb.webp"
    Image.new("RGB", (400, 300), color=(4, 5, 6)).save(thumb, format="WEBP")
    db = SessionLocal()
    a = Asset(id=ids.asset_id(), project_id=pid, asset_type="keyframe", name="a",
              version=1, file_path=str(src), thumb_path=str(thumb), validity="current")
    db.add(a)
    db.commit()
    aid = a.id
    db.close()
    # 原文件
    r = client.get(f"/api/assets/{aid}/file")
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    # 缩略图
    r = client.get(f"/api/assets/{aid}/file", params={"thumb": "true"})
    assert r.status_code == 200 and b"WEBP" in r.content[:64]
    # 缩略图缺失回退原文件
    db = SessionLocal()
    a = db.get(Asset, aid)
    a.thumb_path = None
    db.commit()
    db.close()
    r = client.get(f"/api/assets/{aid}/file", params={"thumb": "true"})
    assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
    # 文件不存在 → 404
    db = SessionLocal()
    a = db.get(Asset, aid)
    a.file_path = str(tmp_path / "missing.png")
    db.commit()
    db.close()
    assert client.get(f"/api/assets/{aid}/file").status_code == 404
