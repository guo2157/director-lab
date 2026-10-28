"""资产定义落库验证：解析确认后名称、占位路径、提示词与参考依赖一次性落库。

验证点：
- 角色/场景/道具/分镜图/分镜视频五类定义均生成，状态为待生成
- 名称唯一且路径固定（同项目内不重复、重复调用不改变名称与路径）
- 分镜图提示词引用已定稿的场景/角色资产名
- 分镜视频依赖指向对应分镜图定义
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
    from sqlalchemy.orm import sessionmaker
    new_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal", new_session)
    import app.worker.parse_executor as pe_mod
    monkeypatch.setattr(pe_mod, "SessionLocal", new_session)
    return TestClient(app)


SCRIPT_TEXT = "第一集。林晓走进教室，对同桌说：你看见我的笔记本了吗？ 同桌摇头。窗外下起了雨。" * 30

FAKE_PARSE_OUTPUT = {
    "project_analysis": {"summary": "教室悬疑", "theme": "信任", "highlights": ["反转"], "foreshadowing": []},
    "characters": [{"name": "林晓", "description": "转学生", "traits": "细心", "appearance": "短发"}],
    "props": [{"name": "笔记本", "description": "蓝色封面", "appearance": "磨损"}],
    "scenes": [{
        "episode": 1, "title": "教室", "description": "午后教室", "time_note": "白天",
        "shots": [
            {"episode": 1, "seq": 1, "scene_id": "教室", "character_id": "林晓",
             "dialogue": "你看见我的笔记本了吗？", "duration": 5,
             "image_prompt": "教室里的林晓", "shot_kind": "dialogue"},
            {"episode": 1, "seq": 2, "scene_id": "教室", "duration": 4,
             "image_prompt": "窗外的雨", "shot_kind": "visual"},
        ],
    }],
    "episodes": [{"episode": 1, "goal": "建立悬念", "rhythm_curve": "平稳",
                  "cliffhanger_type": "信息缺失", "hook_to_next": "笔记本下落", "locked": False}],
}


def _run_parse_and_confirm(client, monkeypatch):
    pid = client.post("/api/projects", json={"name": "定义验证", "aspect_ratio": "9:16"}).json()["id"]
    sv = client.post(f"/api/projects/{pid}/script-versions", json={"text": SCRIPT_TEXT}).json()
    r = client.post(f"/api/projects/{pid}/parse",
                    json={"script_version_id": sv["script_version_id"], "text": SCRIPT_TEXT,
                          "episode_start": 1, "episode_count": 1})
    job_id = r.json()["job_id"]
    import app.worker.parse_executor as pe_mod

    class _FakeLLM:
        def __init__(self, *a, **kw):
            pass

        def chat_json(self, s, u):
            return FAKE_PARSE_OUTPUT

    monkeypatch.setattr(pe_mod, "LLMClient", _FakeLLM)
    assert pe_mod.execute_parse_job(job_id)["ok"] is True
    pv_id = client.get(f"/api/jobs/{job_id}").json()["result"]["parse_version_id"]
    r = client.post(f"/api/projects/{pid}/parse-versions/{pv_id}/confirm",
                    json={"current_parse_version_id": None})
    assert r.status_code == 200, r.text
    return pid


def test_asset_plans_written_after_confirm(client, monkeypatch):
    """解析确认后五类定义与依赖落库，状态为待生成。"""
    pid = _run_parse_and_confirm(client, monkeypatch)
    from app.core.database import SessionLocal
    from app.models import AssetPlan, AssetPlanDependency

    db = SessionLocal()
    try:
        plans = db.query(AssetPlan).filter(AssetPlan.project_id == pid).all()
        assert plans, "解析确认后未生成任何资产定义"
        by_type = {}
        for p in plans:
            by_type.setdefault(p.plan_type, []).append(p)
        for t in ("character", "scene", "prop", "keyframe", "clip"):
            assert by_type.get(t), f"缺少 {t} 类型定义"
        # 状态为待生成，且尚未绑定正式资产
        assert all(p.status == "pending" for p in plans)
        assert all(p.asset_id is None for p in plans)
        # 名称唯一
        keys = [p.asset_key for p in plans]
        assert len(keys) == len(set(keys)), "存在重复资产名称"
        # 路径固定且唯一
        paths = [p.media_path for p in plans]
        assert len(paths) == len(set(paths)), "存在重复占位路径"
        assert all(p.asset_key in p.media_path for p in plans)

        # 分镜图提示词用 @名称 引用场景/角色（@引用解析可携带参考图）
        kf = by_type["keyframe"][0]
        scene = by_type["scene"][0]
        char = by_type["character"][0]
        assert f"@{scene.asset_key}" not in (kf.prompt or ""), "提示词不应再使用资产键引用"
        assert "@教室" in (kf.prompt or ""), f"分镜图提示词未引用场景名: {kf.prompt}"
        assert "@林晓" in (kf.prompt or ""), f"分镜图提示词未引用角色名: {kf.prompt}"

        # 分镜视频依赖指向对应分镜图
        clip = by_type["clip"][0]
        deps = db.query(AssetPlanDependency).filter(AssetPlanDependency.plan_id == clip.id).all()
        slots = {d.slot: d for d in deps}
        assert "ref_image" in slots, f"分镜视频缺少分镜图依赖: {list(slots)}"
        assert slots["ref_image"].ref_key == kf.asset_key
    finally:
        db.close()


def test_asset_plan_keys_stable_on_reparse(client, monkeypatch):
    """重复生成定义不改变既有名称与路径。"""
    pid = _run_parse_and_confirm(client, monkeypatch)
    from app.core.database import SessionLocal
    from app.models import AssetPlan
    from app.services.asset_plan import build_asset_plans
    from app.models import Project

    db = SessionLocal()
    try:
        before = {p.id: (p.asset_key, p.media_path) for p in
                  db.query(AssetPlan).filter(AssetPlan.project_id == pid).all()}
        project = db.get(Project, pid)
        build_asset_plans(db, project, episode_start=1, episode_count=1)
        db.commit()
        after = {p.id: (p.asset_key, p.media_path) for p in
                 db.query(AssetPlan).filter(AssetPlan.project_id == pid).all()}
        assert before == after, "重复生成改变了既有名称或路径"
    finally:
        db.close()
