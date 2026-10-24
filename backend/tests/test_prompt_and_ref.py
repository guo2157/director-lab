"""第三期测试（D-15 提示词外置 / D-16 @引用注入）。

提示词外置：
- 列出/读取/写入/重置端点；写入保存即生效（get_prompt 动态读取）
- 文件缺失时回退内置默认值

@引用注入：
- extract_refs 提取 @名称（去重保序）
- resolve_refs：角色→定妆图、场景→基准图；未匹配 422、资产缺失 409
- build_media_params：仅注入工作流声明槽位
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


# ---------- D-15 提示词外置 ----------

def test_prompt_store_fallback_and_write(tmp_path, monkeypatch):
    """文件缺失回退内置默认值；写入后 get_prompt 立即返回新内容。"""
    import app.services.prompt_store as ps
    monkeypatch.setattr(ps, "PROMPT_DIR", tmp_path / "prompts")
    # 内置默认值（经 script_parse 注册）
    from app.services.script_parse import PARSE_SYSTEM_PROMPT
    assert ps.get_prompt("parse_system") == PARSE_SYSTEM_PROMPT
    # 写入后立即生效
    ps.write_prompt("parse_system", "新提示词内容")
    assert ps.get_prompt("parse_system") == "新提示词内容"
    # 重置回默认值
    ps.reset_prompt("parse_system")
    assert ps.get_prompt("parse_system") == PARSE_SYSTEM_PROMPT


def test_prompts_api_endpoints(client: TestClient):
    """提示词管理端点：列出/读取/写入/重置。"""
    lst = client.get("/api/settings/prompts")
    assert lst.status_code == 200
    names = [p["name"] for p in lst.json()]
    assert "parse_system" in names
    # 读取
    r = client.get("/api/settings/prompts/parse_system")
    assert r.status_code == 200 and "content" in r.json()
    # 写入
    r = client.put("/api/settings/prompts/parse_system", json={"content": "测试提示词 v2"})
    assert r.status_code == 200
    assert client.get("/api/settings/prompts/parse_system").json()["content"] == "测试提示词 v2"
    # 不存在的提示词
    assert client.get("/api/settings/prompts/nonexistent").status_code == 404
    assert client.put("/api/settings/prompts/nonexistent", json={"content": "x"}).status_code == 404
    # 空内容
    assert client.put("/api/settings/prompts/parse_system", json={"content": ""}).status_code == 400
    # 重置
    assert client.post("/api/settings/prompts/parse_system/reset").status_code == 200


# ---------- D-16 @引用注入 ----------

def test_extract_refs():
    from app.services.ref_inject import extract_refs
    assert extract_refs("教室里的 @林晓 和 @操场") == ["林晓", "操场"]
    assert extract_refs("@林晓 再次出现 @林晓") == ["林晓"]
    assert extract_refs("") == []


def test_resolve_refs_character_and_scene(client: TestClient):
    """角色→定妆图、场景→基准图；未匹配 422；资产缺失 409。"""
    from fastapi import HTTPException

    from app.core.database import SessionLocal
    from app.models import Asset, Character, Scene, ids
    from app.services import ref_inject

    pid = client.post("/api/projects", json={"name": "引用项目", "aspect_ratio": "9:16"}).json()["id"]
    db = SessionLocal()
    # 角色（有定妆图）与场景（无基准图）
    a = Asset(id=ids.asset_id(), project_id=pid, asset_type="keyframe", name="portrait",
              version=1, file_path="/tmp/p.png", validity="current")
    db.add(a)
    db.flush()
    ch = Character(id=ids.character_id(), project_id=pid, name="林晓", appearance="短发",
                   portrait_asset_id=a.id)
    scn = Scene(id=ids.scene_id(), project_id=pid, title="教室", seq=1)
    db.add_all([ch, scn])
    db.commit()

    refs = ref_inject.resolve_refs(db, pid, "教室里的 @林晓")
    assert refs["林晓"]["type"] == "character"
    assert refs["林晓"]["file_path"] == "/tmp/p.png"

    # 场景无基准图 → 409
    with pytest.raises(HTTPException) as ei:
        ref_inject.resolve_refs(db, pid, "@教室")
    assert ei.value.status_code == 409

    # 未匹配 → 422
    with pytest.raises(HTTPException) as ei:
        ref_inject.resolve_refs(db, pid, "@不存在")
    assert ei.value.status_code == 422
    db.close()


def test_build_media_params_slot_filter(client: TestClient):
    """仅注入工作流声明槽位：未声明 ref_scene 时场景引用被跳过。"""
    from app.core.database import SessionLocal
    from app.models import Asset, Character, Scene, ids
    from app.services import ref_inject

    pid = client.post("/api/projects", json={"name": "槽位项目", "aspect_ratio": "9:16"}).json()["id"]
    db = SessionLocal()
    a1 = Asset(id=ids.asset_id(), project_id=pid, asset_type="keyframe", name="p1",
               version=1, file_path="/tmp/p1.png", validity="current")
    a2 = Asset(id=ids.asset_id(), project_id=pid, asset_type="keyframe", name="p2",
               version=1, file_path="/tmp/p2.png", validity="current")
    db.add_all([a1, a2])
    db.flush()
    db.add_all([
        Character(id=ids.character_id(), project_id=pid, name="林晓", appearance="短发",
                  portrait_asset_id=a1.id),
        Scene(id=ids.scene_id(), project_id=pid, title="教室", seq=1, baseline_asset_id=a2.id),
    ])
    db.commit()

    # 声明两个槽位 → 全部注入
    media = ref_inject.build_media_params(db, pid, "@教室 里的 @林晓", ["ref_identity", "ref_scene"])
    assert media == {"ref_identity": "/tmp/p1.png", "ref_scene": "/tmp/p2.png"}
    # 仅声明 ref_identity → 场景引用无可用槽位，按新语义返回 422（不静默丢参考）
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        ref_inject.build_media_params(db, pid, "@教室 里的 @林晓", ["ref_identity"])
    assert ei.value.status_code == 422
    # 仅角色引用 → 正常注入
    media = ref_inject.build_media_params(db, pid, "@林晓", ["ref_identity"])
    assert media == {"ref_identity": "/tmp/p1.png"}
    # 无槽位声明（None）→ 全部注入
    media = ref_inject.build_media_params(db, pid, "@林晓", None)
    assert media == {"ref_identity": "/tmp/p1.png"}
    db.close()


def test_resolve_refs_prop(client: TestClient):
    """道具→锚定图；锚定图缺失 409；未匹配 422；与角色同名 422。"""
    from fastapi import HTTPException
    from app.core.database import SessionLocal
    from app.models import Asset, Character, Prop, ids
    from app.services import ref_inject
    pid = client.post("/api/projects", json={"name": "道具引用项目", "aspect_ratio": "9:16"}).json()["id"]
    db = SessionLocal()
    a = Asset(id=ids.asset_id(), project_id=pid, asset_type="keyframe", name="anchor",
              version=1, file_path="/tmp/prop.png", validity="current")
    db.add(a)
    db.flush()
    # 道具（有锚定图）与无锚定图道具
    p1 = Prop(id=ids.prop_id(), project_id=pid, name="古剑", appearance="青铜古剑",
              anchor_asset_id=a.id)
    p2 = Prop(id=ids.prop_id(), project_id=pid, name="玉佩", appearance="白玉佩")
    db.add_all([p1, p2])
    db.commit()
    refs = ref_inject.resolve_refs(db, pid, "手持 @古剑")
    assert refs["古剑"]["type"] == "prop"
    assert refs["古剑"]["file_path"] == "/tmp/prop.png"
    # 道具无锚定图 → 409
    with pytest.raises(HTTPException) as ei:
        ref_inject.resolve_refs(db, pid, "@玉佩")
    assert ei.value.status_code == 409
    # 未匹配 → 422
    with pytest.raises(HTTPException) as ei:
        ref_inject.resolve_refs(db, pid, "@不存在")
    assert ei.value.status_code == 422
    # 道具与角色同名 → 422
    ch = Character(id=ids.character_id(), project_id=pid, name="古剑", appearance="同名角色",
                   portrait_asset_id=a.id)
    db.add(ch)
    db.commit()
    with pytest.raises(HTTPException) as ei:
        ref_inject.resolve_refs(db, pid, "@古剑")
    assert ei.value.status_code == 422
    db.close()


def test_build_media_params_prop_slot(client: TestClient):
    """道具引用注入 ref_prop 槽位；工作流未声明 ref_prop 时 422。"""
    from fastapi import HTTPException
    from app.core.database import SessionLocal
    from app.models import Asset, Prop, ids
    from app.services import ref_inject
    pid = client.post("/api/projects", json={"name": "道具槽位项目", "aspect_ratio": "9:16"}).json()["id"]
    db = SessionLocal()
    a = Asset(id=ids.asset_id(), project_id=pid, asset_type="keyframe", name="anchor",
              version=1, file_path="/tmp/prop.png", validity="current")
    db.add(a)
    db.flush()
    db.add(Prop(id=ids.prop_id(), project_id=pid, name="古剑", appearance="青铜古剑",
                anchor_asset_id=a.id))
    db.commit()
    # 声明 ref_prop → 注入
    media = ref_inject.build_media_params(db, pid, "手持 @古剑", ["ref_prop"])
    assert media == {"ref_prop": "/tmp/prop.png"}
    # 未声明 ref_prop → 422（不静默丢参考）
    with pytest.raises(HTTPException) as ei:
        ref_inject.build_media_params(db, pid, "手持 @古剑", ["ref_identity"])
    assert ei.value.status_code == 422
    # 无槽位声明（None）→ 注入
    media = ref_inject.build_media_params(db, pid, "手持 @古剑", None)
    assert media == {"ref_prop": "/tmp/prop.png"}
    db.close()
