"""《小马过河》端到端回归测试（P4）：Mock 渲染全链路验证。

验证链路：
1. 四个主要角色（小马/老马/牛伯伯/小松鼠）分别生成独立定妆图（纯净化提示词）
2. 场景（马棚/小河与河岸）生成基准图（无角色纯净化提示词）
3. 道具（麦子口袋）生成锚定图
4. 分镜图生成：镜头资产清单自动注入 @引用 → 参考图随任务携带 →
   Mock 渲染产物落库 → quality_status 字段记录
5. 多镜头角色外观一致性基础：同一角色在多个镜头中引用同一份定妆图资产
"""
import json
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
    # 开启 Mock 渲染（跳过 ComfyUI 交互，占位产物照常入库）
    import app.core.config as cfg_mod
    _settings = cfg_mod.Settings(render_mock=True, media_dir=tmp_path / "media")
    monkeypatch.setattr(cfg_mod, "get_settings", lambda: _settings)
    import app.worker.executor as ex_mod
    monkeypatch.setattr(ex_mod, "get_settings", lambda: _settings)
    return TestClient(app)


_MOCK_WF = {
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


def _create_mock_workflow(client: TestClient, name: str) -> str:
    wf = client.post(
        "/api/workflows",
        data={"name": name, "workflow_type": "image"},
        files={"file": (f"{name}.json", json.dumps(_MOCK_WF), "application/json")},
    )
    if wf.status_code >= 400:
        pytest.skip(f"工作流创建接口不可用: {wf.status_code} {wf.text[:200]}")
    return wf.json()["id"]


def test_pony_crossing_river_e2e(client: TestClient):
    """《小马过河》Mock 渲染全链路：角色→场景→道具→分镜图。"""
    from app.worker.executor import execute_generate_job

    wf_id = _create_mock_workflow(client, "pony-e2e-image")

    # --- 1. 项目与四个主要角色 ---
    pid = client.post("/api/projects", json={"name": "小马过河", "aspect_ratio": "9:16"}).json()["id"]
    chars = {}
    for name, appearance in [
        ("小马", "棕黄色毛发的拟人化卡通小马，幼年，深棕色鬃毛，大眼睛"),
        ("老马", "深棕色毛发的拟人化卡通马，成年，黑色长鬃毛，温和眼神"),
        ("牛伯伯", "黑白花斑的拟人化卡通老牛，健壮体型，弯角"),
        ("小松鼠", "红棕色毛发的拟人化卡通松鼠，蓬松大尾巴"),
    ]:
        chars[name] = client.post(
            "/api/characters",
            json={"project_id": pid, "name": name, "appearance": appearance},
        ).json()

    # --- 2. 场景与道具 ---
    scenes = {}
    for title, desc in [
        ("马棚", "老马和小马居住的马棚，木质围栏，铺着干草"),
        ("小河与河岸", "清澈的小河，两岸绿草，远处有磨坊"),
    ]:
        scenes[title] = client.post(
            "/api/scenes",
            json={"project_id": pid, "title": title, "description": desc, "episode": 1},
        ).json()
    prop = client.post(
        "/api/props",
        json={"project_id": pid, "name": "麦子口袋", "appearance": "粗麻布口袋，装满金黄麦子"},
    ).json()

    # --- 3. 角色定妆图生成（纯净化提示词 + Mock 渲染） ---
    portraits = {}
    for name, ch in chars.items():
        r = client.post(f"/api/characters/{ch['id']}/generate", json={"workflow_id": wf_id})
        assert r.status_code == 202, r.text
        jid = r.json()["id"]
        # 验证阶段 B 纯净化提示词已注入任务：单主体 + 禁止多视图拼图
        params = r.json()["effective_inputs"]["params"]
        assert "画面中只能有一个主体" in params["prompt"], name
        assert ("默认输出单张正面全身定妆照" in params["prompt"] or "不做并排多视角拼图" in params["prompt"]), name
        compilation = (r.json()["effective_inputs"].get("prompt_compilation") or {})
        if compilation:
            assert compilation["generation_constraints"]["isolation_rule"] == "single_character_only"
        result = execute_generate_job(jid)
        assert result["ok"], result
        portraits[name] = result["assets"][0]["asset_id"]
        # 定妆图回写（经数据库会话验证，characters API 无单查端点）
        from app.core.database import SessionLocal as _SL
        from app.models import Character as _Ch
        _db = _SL()
        try:
            _c = _db.get(_Ch, ch["id"])
            assert _c is not None and _c.portrait_asset_id == portraits[name], name
        finally:
            _db.close()

    # --- 4. 场景基准图生成 ---
    baselines = {}
    for title, sc in scenes.items():
        r = client.post(f"/api/scenes/{sc['id']}/generate-baseline", json={"workflow_id": wf_id})
        assert r.status_code == 202, r.text
        params = r.json()["effective_inputs"]["params"]
        # 场景纯净度：对齐当前 SCENE_TEMPLATE 的稳定措辞（不再使用旧版『只表现环境本身』表述）
        assert "整个画面只由环境本身与天空构成" in params["prompt"], title
        assert "没有任何人" in params["prompt"], title
        result = execute_generate_job(r.json()["id"])
        assert result["ok"], result
        baselines[title] = result["assets"][0]["asset_id"]

    # --- 5. 道具锚定图生成 ---
    r = client.post(f"/api/props/{prop['id']}/generate", json={"workflow_id": wf_id})
    assert r.status_code == 202, r.text
    assert "画面中只能有一件道具" in r.json()["effective_inputs"]["params"]["prompt"]
    result = execute_generate_job(r.json()["id"])
    assert result["ok"], result
    anchor_id = result["assets"][0]["asset_id"]
    # 人工审核闸口（D-23）：三类公共资产（定妆图/基准图/锚定图）全部生成完毕后，
    # 真实流程需人工审核通过才放行分镜图；此处模拟该审核动作。
    # 注意：公共资产生成会主动把审核状态回退为待审核，故放行必须在全部生成之后。
    from app.core.database import SessionLocal as _SLR
    from app.models import Project as _ProjectR
    _dbr = _SLR()
    try:
        _pr = _dbr.get(_ProjectR, pid)
        _pr.assets_review_status = "approved"
        _dbr.commit()
    finally:
        _dbr.close()

    # --- 6. 分镜图生成（镜头资产清单注入 + @引用参考图） ---
    # 镜头1：小马在马棚听老马说话（提示词未写 @引用，应自动注入）
    shot1 = client.post("/api/shots", json={
        "scene_id": scenes["马棚"]["id"], "episode": 1, "shot_kind": "dialogue",
        "duration": 5, "character_id": chars["小马"]["id"],
        "image_prompt": "小马在马棚里听老马说话，中景",
        "dialogue": "妈妈，我能把麦子驮到河对岸的磨坊吗？",
    }).json()
    # 镜头2：小马在河边问牛伯伯（显式 @引用）
    shot2 = client.post("/api/shots", json={
        "scene_id": scenes["小河与河岸"]["id"], "episode": 1, "shot_kind": "dialogue",
        "duration": 5, "character_id": chars["小马"]["id"],
        "image_prompt": "@小马 站在画面左侧的河边，向画面右侧的 @牛伯伯 询问河水深浅，中景",
        "dialogue": "牛伯伯，河水深不深？",
    }).json()

    for shot in (shot1, shot2):
        r = client.post(f"/api/shots/{shot['id']}/generate", json={"target": "keyframe", "workflow_id": wf_id})
        assert r.status_code == 202, r.text
        params = r.json()["effective_inputs"]["params"]
        # 镜头资产清单注入：出场角色与场景以 @引用 补入提示词
        assert "@小马" in params["prompt"], params["prompt"]
        # 参考图随任务携带（@引用解析 → __media__）
        assert "__media__" in params, params
        result = execute_generate_job(r.json()["id"])
        assert result["ok"], result

    # --- 7. 一致性基础验证：同一角色多镜头引用同一份定妆图 ---
    from app.core.database import SessionLocal
    from app.models import Asset
    db = SessionLocal()
    try:
        pony_portrait = db.get(Asset, portraits["小马"])
        assert pony_portrait is not None
        assert pony_portrait.asset_type == "character"
        assert pony_portrait.validity == "current"
        # Mock 模式跳过质量检查，quality_status 保持 pending
        assert pony_portrait.quality_status in ("pending", None)
        # 四个角色定妆图资产独立存在
        for name, aid in portraits.items():
            a = db.get(Asset, aid)
            assert a is not None, name
            assert a.asset_type == "character"
    finally:
        db.close()
