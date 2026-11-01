"""预览媒体接口测试：/api/media/{path} 的路径解析与越界防护。

回归背景：合成记录早期存的是相对后端工作目录的路径（data/media/cmp_xxx.mp4），
前端直接拼 /api/media/ 会变成 data/media/data/media/... 返回 400，预览页黑屏。
接口须同时接受「相对 media_dir」与「相对工作目录」两种口径，且不得越出 media_dir。
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def media_env(tmp_path, monkeypatch):
    media_dir = tmp_path / "data" / "media"
    media_dir.mkdir(parents=True)
    (media_dir / "cmp_demo.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
    monkeypatch.setenv("DLAB_MEDIA_DIR", str(media_dir))
    import app.core.config as cfg_mod
    cfg_mod.clear_settings_cache()
    monkeypatch.chdir(tmp_path)  # 让 data/media/cmp_demo.mp4 相对路径成立
    yield TestClient(app), media_dir
    cfg_mod.clear_settings_cache()


def test_serves_path_relative_to_media_dir(media_env):
    client, _ = media_env
    r = client.get("/api/media/cmp_demo.mp4")
    assert r.status_code == 200
    assert r.content.startswith(b"\x00\x00\x00\x18ftyp")


def test_serves_legacy_path_with_media_prefix(media_env):
    """历史数据：data/media/cmp_demo.mp4 也应能直接访问（前端不再需要自己裁前缀）。"""
    client, _ = media_env
    r = client.get("/api/media/data/media/cmp_demo.mp4")
    assert r.status_code == 200


def test_rejects_missing_file(media_env):
    client, _ = media_env
    assert client.get("/api/media/nope.mp4").status_code >= 400


def test_rejects_path_traversal(media_env):
    client, media_dir = media_env
    secret = Path(media_dir).parent.parent / "secret.mp4"
    secret.write_bytes(b"secret")
    r = client.get("/api/media/../secret.mp4")
    assert r.status_code >= 400
    assert b"secret" not in r.content
