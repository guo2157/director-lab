"""资产纯净化与分镜一致性专项改造测试（P0-P3）。

覆盖：
- P0：场景批量生成接口的 @引用注入（参考图随任务携带）
- P1：角色/场景/道具纯净化提示词模板（单一主体/干净背景/负面约束分段）
- P2：镜头资产清单注入（出场角色/场景未 @引用 时自动补入；无资产图不注入）
- P3：图片质量检查（空白图拒绝/正常图通过/损坏文件拒绝）与质量失败重试
"""
import io
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


# ---------- P1：纯净化提示词模板 ----------

def test_character_prompt_purification():
    from app.services.asset_prompts import build_character_prompt, negative_for_asset_type
    p = build_character_prompt("小马", "棕黄色毛发的拟人化卡通小马，鬃毛深棕色")
    assert "只出现这一个指定角色" in p
    assert "第二个人物" in p
    assert "干净" in p and "背景" in p
    assert "小马" in p
    for t in ("character", "scene", "prop"):
        assert negative_for_asset_type(t)


def test_scene_prompt_no_characters():
    from app.services.asset_prompts import build_scene_prompt
    p = build_scene_prompt("小河与河岸", "清澈的小河，两岸绿草")
    assert "不出现任何人物" in p
    assert "不出现剧情中的可移动道具、手持物品、交通工具或临时摆放物品" in p


def test_prop_prompt_single_subject():
    from app.services.asset_prompts import build_prop_prompt
    p = build_prop_prompt("麦子口袋", "粗麻布口袋，装满金黄麦子")
    assert "只出现这一个指定道具" in p
    assert "不出现角色、手部" in p


# ---------- P2：镜头资产清单注入 ----------

def test_inject_asset_refs_adds_missing_refs(client: TestClient):
    """出场角色/场景未 @引用 时自动补入；无资产图的角色不注入。"""
    from app.core.database import SessionLocal
    from app.models import Asset, Character, Scene, Shot, ids
    from app.services.shot_refs import inject_asset_refs

    pid = client.post("/api/projects", json={"name": "p2test", "aspect_ratio": "9:16"}).json()["id"]
    db = SessionLocal()
    try:
        a = Asset(id=ids.asset_id(), project_id=pid, asset_type="character",
                  name="小马", version=1, file_path="/tmp/x.png", validity="current")
        db.add(a)
        db.flush()
        pony = Character(id=ids.character_id(), project_id=pid, name="小马",
                         portrait_asset_id=a.id)
        old = Character(id=ids.character_id(), project_id=pid, name="老马")
        scene = Scene(id=ids.scene_id(), project_id=pid, episode=1, seq=1,
                      title="马棚", baseline_asset_id="ast_s1")
        db.add_all([pony, old, scene])
        db.flush()
        shot = Shot(id=ids.shot_id(), scene_id=scene.id, episode=1, seq=1,
                     character_id=pony.id, shot_kind="dialogue", duration=5,
                     image_prompt="小马在马棚里听老马说话",
                     dialogue_segments=[{"speaker": "小马", "text": "妈妈"}, {"speaker": "老马", "text": "去吧"}])
        db.add(shot)
        db.commit()

        out = inject_asset_refs(db, shot, shot.image_prompt)
        assert "@小马" in out
        assert "@马棚" in out
        assert "@老马" not in out
        assert out.endswith("小马在马棚里听老马说话")
    finally:
        db.close()


def test_inject_asset_refs_no_duplicate(client: TestClient):
    """已显式 @引用 的实体不重复注入。"""
    from app.core.database import SessionLocal
    from app.models import Asset, Character, Scene, Shot, ids
    from app.services.shot_refs import inject_asset_refs

    pid = client.post("/api/projects", json={"name": "p2dup", "aspect_ratio": "9:16"}).json()["id"]
    db = SessionLocal()
    try:
        a = Asset(id=ids.asset_id(), project_id=pid, asset_type="character",
                  name="小马", version=1, file_path="/tmp/x.png", validity="current")
        db.add(a)
        db.flush()
        pony = Character(id=ids.character_id(), project_id=pid, name="小马",
                         portrait_asset_id=a.id)
        scene = Scene(id=ids.scene_id(), project_id=pid, episode=1, seq=1,
                      title="马棚", baseline_asset_id="ast_s1")
        db.add_all([pony, scene])
        db.flush()
        shot = Shot(id=ids.shot_id(), scene_id=scene.id, episode=1, seq=1,
                     character_id=pony.id, shot_kind="dialogue", duration=5,
                     image_prompt="@小马 在 @马棚 里吃草")
        db.add(shot)
        db.commit()
        out = inject_asset_refs(db, shot, shot.image_prompt)
        assert out == shot.image_prompt
    finally:
        db.close()


# ---------- P3：图片质量检查 ----------

def _make_png(path, color, size=(512, 512)):
    from PIL import Image
    Image.new("RGB", size, color).save(path, format="PNG")


def test_quality_check_blank_image_rejected(tmp_path):
    from app.services.image_quality import check_image_quality
    p = tmp_path / "blank.png"
    _make_png(p, (200, 200, 200))
    r = check_image_quality(str(p))
    assert not r["passed"]
    assert any("纯色" in i for i in r["issues"])


def test_quality_check_normal_image_passed(tmp_path):
    from PIL import Image, ImageDraw
    import random
    from app.services.image_quality import check_image_quality
    p = tmp_path / "normal.png"
    img = Image.new("RGB", (1024, 1024), (240, 240, 240))
    d = ImageDraw.Draw(img)
    # 使用随机噪声填充中心区域，确保文件大小超过下限且边缘留白
    random.seed(42)
    for y in range(32, 992):
        for x in range(32, 992, 2):
            d.point([(x, y)], fill=(random.randint(0, 255), random.randint(0, 255), random.randint(0, 255)))
    img.save(p, format="PNG")
    r = check_image_quality(str(p))
    assert r["passed"], r["issues"]


def test_quality_check_missing_file_rejected(tmp_path):
    from app.services.image_quality import check_image_quality
    r = check_image_quality(str(tmp_path / "nope.png"))
    assert not r["passed"]


def test_quality_check_tiny_image_rejected(tmp_path):
    from app.services.image_quality import check_image_quality
    p = tmp_path / "tiny.png"
    _make_png(p, (100, 50, 50), size=(64, 64))
    r = check_image_quality(str(p))
    assert not r["passed"]
    assert any("尺寸过小" in i for i in r["issues"])


# ---------- P0：场景批量生成 @引用注入 ----------

def test_scene_batch_generate_injects_media(client: TestClient, tmp_path):
    """场景批量生成 keyframe 时，提示词 @角色 的参考图随任务 params 携带。"""
    from app.core.database import SessionLocal
    from app.models import Asset, Character, Scene, Shot, Workflow, ids

    pid = client.post("/api/projects", json={"name": "p0test", "aspect_ratio": "9:16"}).json()["id"]
    db = SessionLocal()
    try:
        # D-23 资产门禁：定妆图文件必须真实存在
        from pathlib import Path
        # 用 tmp_path 隔离：共享 /tmp/x.png 可能被其他进程占用导致写入权限冲突
        _headshot = tmp_path / "x.png"
        _headshot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        a = Asset(id=ids.asset_id(), project_id=pid, asset_type="character",
                  name="小马", version=1, file_path=str(_headshot), validity="current")
        db.add(a)
        db.flush()
        pony = Character(id=ids.character_id(), project_id=pid, name="小马",
                         portrait_asset_id=a.id)
        scene = Scene(id=ids.scene_id(), project_id=pid, episode=1, seq=1, title="马棚")
        db.add_all([pony, scene])
        db.flush()
        # D-23 资产门禁：为场景构造真实基准图资产
        from tests.conftest import seed_scene_baseline_asset
        seed_scene_baseline_asset(db, scene, "/tmp/dlab_test_media")
        for i in range(3):
            db.add(Shot(id=ids.shot_id(), scene_id=scene.id, episode=1, seq=i + 1,
                        shot_kind="visual", duration=5, character_id=pony.id,
                        image_prompt=f"@小马 在马棚，镜头{i+1}"))
        wf = Workflow(id=ids.workflow_id(), name="img", workflow_type="image",
                      capabilities={"reference_slots": ["ref_identity", "ref_scene"]},
                      definition={"nodes": []}, is_default=True)
        db.add(wf)
        # D-23 资产审核闸口：本用例聚焦 @引用参考图注入，
        # 直接放行公共资产人工审核，避免与资产审核前置条件耦合
        from tests.conftest import approve_project_assets
        approve_project_assets(db, pid)
        db.commit()
        scene_id = scene.id
    finally:
        db.close()

    r = client.post(f"/api/scenes/{scene_id}/generate", json={"target": "keyframe"})
    assert r.status_code == 202, r.text
    jobs = r.json()["jobs"]
    assert len(jobs) == 3
    for j in jobs:
        params = j["effective_inputs"]["params"]
        assert "__media__" in params
        assert "ref_identity" in params["__media__"]
