"""Phase 12 E2E 样例 A：短剧本快速路径（mock LLM + mock 渲染）。

链路：
剧本版本 → 大纲评估 → 解析任务 → 实体落库 → 公共资产生成与审核 →
分镜规划只读校验 → 分镜图 → 视频 → 只读影响预检。

约束：
- 不依赖真实 LLM / ComfyUI / GPU；LLM 与渲染均为替身；
- 所有写入均由显式 API 触发，规划校验保持 dry-run；
- 真实 ComfyUI/GPU 验收需按人工验收清单单独执行，不在本测试范围。
"""
import json

import pytest
from fastapi.testclient import TestClient

from app.core.database import make_engine
from app.main import app
from app.models import Base

SCRIPT_TEXT = (
    "第一集\n"
    "第一场 教室 日 内\n"
    "旁白：雨停了。\n"
    "林晓推门走进教室，对同桌说：\"你看见我的笔记本了吗？\"\n"
    "同桌摇头。窗外下起了雨。\n"
    "第二场 走廊 夜 内\n"
    "林晓独自离开，走廊尽头灯忽明忽暗。\n"
)

FAKE_OUTLINE = {
    "total_episodes": 1,
    "estimated_total_duration": 90,
    "dialogue_char_estimate": 50,
    "episodes": [
        {"episode": 1, "title": "第一集", "estimated_duration": 90, "estimated_shots": 9},
    ],
}

FAKE_PARSE = {
    "project_analysis": {"summary": "教室悬疑", "theme": "信任", "highlights": [], "foreshadowing": []},
    "characters": [{"name": "林晓", "description": "转学生", "traits": "细心", "appearance": "短发"}],
    "props": [{"name": "笔记本", "description": "蓝色封面", "appearance": "磨损"}],
    "scenes": [
        {
            "episode": 1,
            "title": "教室",
            "description": "午后教室",
            "time_note": "白天",
            "shots": [
                {"episode": 1, "seq": 1, "scene_id": "教室", "character_id": "林晓",
                 "dialogue": "你看见我的笔记本了吗？", "duration": 5,
                 "image_prompt": "教室里的 @林晓", "shot_kind": "dialogue"},
                {"episode": 1, "seq": 2, "scene_id": "教室",
                 "duration": 4, "image_prompt": "窗外的雨", "shot_kind": "visual"},
            ],
        },
        {
            "episode": 1,
            "title": "走廊",
            "description": "昏暗走廊",
            "time_note": "夜晚",
            "shots": [
                {"episode": 1, "seq": 1, "scene_id": "走廊", "character_id": "林晓",
                 "duration": 4, "image_prompt": "走廊尽头灯光闪烁", "shot_kind": "visual"},
            ],
        },
    ],
    "episodes": [
        {"episode": 1, "goal": "建立悬念", "rhythm_curve": "平稳",
         "cliffhanger_type": "信息缺失", "hook_to_next": "笔记本下落", "locked": False},
    ],
}

SCRIPT_TEXT_COMPLEX = (
    "第一集\n"
    "第一场 教室 日 内\n"
    "林晓：你看见我的笔记本了吗？\n"
    "周老师：先坐下，慢慢说。\n"
    "林晓翻开书包，笔记本不见了。\n"
    "第二场 走廊 夜 内\n"
    "林晓独自离开，走廊尽头灯忽明忽暗。\n"
    "第二集\n"
    "第一场 屋顶 夜 外\n"
    "林晓站在栏杆边，手里握着钥匙。\n"
    "周老师追上来：把钥匙给我。\n"
)

FAKE_OUTLINE_COMPLEX = {
    "total_episodes": 2,
    "estimated_total_duration": 180,
    "dialogue_char_estimate": 120,
    "episodes": [
        {"episode": 1, "title": "第一集", "estimated_duration": 90, "estimated_shots": 12},
        {"episode": 2, "title": "第二集", "estimated_duration": 90, "estimated_shots": 6},
    ],
}

FAKE_PARSE_COMPLEX = {
    "project_analysis": {"summary": "校园悬疑", "theme": "秘密", "highlights": [], "foreshadowing": []},
    "characters": [
        {"name": "林晓", "description": "转学生", "traits": "执着", "appearance": "短发"},
        {"name": "周老师", "description": "班主任", "traits": "沉稳", "appearance": "眼镜"},
    ],
    "props": [
        {"name": "笔记本", "description": "蓝色封面", "appearance": "磨损"},
        {"name": "钥匙", "description": "旧铜钥匙", "appearance": "锈迹"},
    ],
    "scenes": [
        {
            "episode": 1,
            "title": "教室",
            "description": "午后教室",
            "time_note": "白天",
            "shots": [
                {"episode": 1, "seq": 1, "scene_id": "教室", "character_id": "林晓",
                 "dialogue": "你看见我的笔记本了吗？", "duration": 5,
                 "image_prompt": "教室里的 @林晓", "shot_kind": "dialogue"},
                {"episode": 1, "seq": 2, "scene_id": "教室", "character_id": "周老师",
                 "dialogue": "先坐下，慢慢说。", "duration": 4,
                 "image_prompt": "讲台前的 @周老师", "shot_kind": "dialogue"},
                {"episode": 1, "seq": 3, "scene_id": "教室", "character_id": "林晓",
                 "duration": 4, "image_prompt": "翻找书包的 @林晓", "shot_kind": "action"},
            ],
        },
        {
            "episode": 1,
            "title": "走廊",
            "description": "昏暗走廊",
            "time_note": "夜晚",
            "shots": [
                {"episode": 1, "seq": 1, "scene_id": "走廊", "character_id": "林晓",
                 "duration": 4, "image_prompt": "走廊尽头的 @林晓", "shot_kind": "visual"},
            ],
        },
        {
            "episode": 2,
            "title": "屋顶",
            "description": "雨夜屋顶",
            "time_note": "夜晚",
            "shots": [
                {"episode": 2, "seq": 1, "scene_id": "屋顶", "character_id": "林晓",
                 "duration": 4, "image_prompt": "栏杆边的 @林晓 握着 @钥匙", "shot_kind": "visual"},
                {"episode": 2, "seq": 2, "scene_id": "屋顶", "character_id": "周老师",
                 "dialogue": "把钥匙给我。", "duration": 4,
                 "image_prompt": "追上来的 @周老师", "shot_kind": "dialogue"},
            ],
        },
    ],
    "episodes": [
        {"episode": 1, "goal": "建立悬念", "rhythm_curve": "平稳",
         "cliffhanger_type": "信息缺失", "hook_to_next": "笔记本下落", "locked": False},
        {"episode": 2, "goal": "逼问真相", "rhythm_curve": "上扬",
         "cliffhanger_type": "身份反转", "hook_to_next": "钥匙来历", "locked": False},
    ],
}

_IMAGE_WF = {
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

_VIDEO_WF = {
    "1": {
        "class_type": "EmptyLatentImage",
        "_meta": {"title": "$~width, $~height"},
        "inputs": {"width": 512, "height": 288, "batch_size": 1},
    },
    "2": {
        "class_type": "SaveVideo",
        "_meta": {"title": "$output.clip"},
        "inputs": {"images": ["1", 0]},
    },
}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'pipeline.db'}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod

    monkeypatch.setattr(db_mod, "engine", engine)
    from sqlalchemy.orm import sessionmaker

    new_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", new_session)
    import app.worker.parse_executor as pe_mod

    monkeypatch.setattr(pe_mod, "SessionLocal", new_session)
    # mock 渲染落临时 media_dir，禁止写真实 backend/data/media
    import app.core.config as cfg_mod

    settings = cfg_mod.Settings(render_mock=True, media_dir=tmp_path / "media")
    monkeypatch.setattr(cfg_mod, "get_settings", lambda: settings)
    import app.worker.executor as ex_mod

    monkeypatch.setattr(ex_mod, "get_settings", lambda: settings)
    # 场景板 LLM 编译也走替身，避免测试触达外网（失败会走规则兜底，但不应依赖网络）
    import app.services.scene_plate as plate_mod

    def _no_llm(*args, **kwargs):
        raise RuntimeError("e2e: 禁用外部 LLM")

    monkeypatch.setattr(plate_mod, "_call_llm", _no_llm)
    return TestClient(app)


def _session():
    from app.core.database import SessionLocal

    return SessionLocal()


def _workflow(client: TestClient, name: str, wf_type: str, definition: dict) -> str:
    r = client.post(
        "/api/workflows",
        data={"name": name, "workflow_type": wf_type},
        files={"file": (f"{name}.json", json.dumps(definition), "application/json")},
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _run_parse(client: TestClient, pid: str, sv: dict, monkeypatch, *,
               text: str = SCRIPT_TEXT, payload: dict | None = None,
               outline: dict | None = None, episode_count: int = 1) -> dict:
    """评估 → 解析 → 实体落库，返回解析任务结果。"""
    import app.api.v1.script as script_mod

    outline = outline or FAKE_OUTLINE
    monkeypatch.setattr(script_mod, "analyze_script_outline", lambda *a, **kw: {
        "algorithm_version": "llm-outline-v1.0",
        "char_count": len(text),
        "dialogue_char_estimate": outline["dialogue_char_estimate"],
        "estimated_total_duration": outline["estimated_total_duration"],
        "estimated_episodes": outline["total_episodes"],
        "episode_breakdown": outline["episodes"],
        "exceeds_batch_limit": False,
        "suggestion": None,
    })
    r = client.post(f"/api/projects/{pid}/estimate",
                    json={"script_version_id": sv["script_version_id"], "text": text})
    assert r.status_code == 201, r.text
    r = client.post(f"/api/projects/{pid}/estimates/{r.json()['estimate_id']}/confirm",
                    json={"script_version_id": sv["script_version_id"],
                          "episode_start": 1, "episode_count": episode_count})
    assert r.status_code == 200, r.text

    r = client.post(f"/api/projects/{pid}/parse",
                    json={"script_version_id": sv["script_version_id"], "text": text,
                          "episode_start": 1, "episode_count": episode_count})
    assert r.status_code == 202, r.text
    job_id = r.json()["job_id"]

    import app.worker.parse_executor as pe_mod

    parsed = payload or FAKE_PARSE

    class _FakeLLM:
        def __init__(self, *a, **kw):
            pass

        def chat_json(self, system_prompt, user_prompt):
            return parsed

    monkeypatch.setattr(pe_mod, "LLMClient", _FakeLLM)
    result = pe_mod.execute_parse_job(job_id)
    assert result["ok"] is True, result
    return result


def _generate(client: TestClient, url: str, payload: dict) -> str:
    r = client.post(url, json=payload)
    assert r.status_code == 202, r.text
    return r.json()["id"]


def _execute(job_id: str) -> dict:
    from app.worker.executor import execute_generate_job

    result = execute_generate_job(job_id)
    assert result["ok"] is True, result
    return result


def test_short_script_pipeline_end_to_end(client: TestClient, monkeypatch):
    from app.models import Asset, Character, Prop, Scene, Shot

    # --- 1. 项目与剧本 ---
    pid = client.post("/api/projects", json={"name": "短剧本 E2E", "aspect_ratio": "9:16"}).json()["id"]
    r = client.post(f"/api/projects/{pid}/script-versions", json={"text": SCRIPT_TEXT})
    assert r.status_code == 201, r.text
    sv = r.json()

    # --- 2. 解析 → 实体落库 ---
    parse_result = _run_parse(client, pid, sv, monkeypatch)
    pv_id = parse_result["parse_version_id"]
    assert client.get(f"/api/projects/{pid}").json()["current_parse_version_id"] == pv_id

    db = _session()
    try:
        characters = db.query(Character).filter(Character.project_id == pid).all()
        props = db.query(Prop).filter(Prop.project_id == pid).all()
        scenes = db.query(Scene).filter(Scene.project_id == pid).order_by(Scene.seq).all()
        shots = (
            db.query(Shot)
            .join(Scene, Shot.scene_id == Scene.id)
            .filter(Scene.project_id == pid)
            .order_by(Shot.seq)
            .all()
        )
        assert [c.name for c in characters] == ["林晓"]
        assert [p.name for p in props] == ["笔记本"]
        assert [s.title for s in scenes] == ["教室", "走廊"]
        assert len(shots) == 3
        character_id, prop_id = characters[0].id, props[0].id
        shot_ids = [shot.id for shot in shots]
        shot_snapshot = [
            (shot.id, shot.seq, shot.duration, shot.dialogue, shot.image_prompt) for shot in shots
        ]
    finally:
        db.close()

    # --- 3. 公共资产：定妆图 / 基准图 / 锚定图 ---
    image_wf = _workflow(client, "e2e-image", "image", _IMAGE_WF)
    _execute(_generate(client, f"/api/characters/{character_id}/generate", {"workflow_id": image_wf}))
    for scene in scenes:
        _execute(_generate(client, f"/api/scenes/{scene.id}/generate-baseline",
                           {"workflow_id": image_wf}))
    _execute(_generate(client, f"/api/props/{prop_id}/generate", {"workflow_id": image_wf}))

    # 人工审核闸口（D-23）：全部公共资产生成后再放行分镜图
    db = _session()
    try:
        from app.models import Project

        project = db.get(Project, pid)
        project.assets_review_status = "approved"
        db.commit()
    finally:
        db.close()

    # --- 4. 分镜规划只读校验（dry-run，不写库） ---
    structure = client.get(
        f"/api/projects/{pid}/script-versions/{sv['script_version_id']}/analysis/shadow-run",
    ).json()["structure"]
    plan_shots = []
    # structure 的 scene_id 是资产定义的 asc_*，写入规划必须用真实 Scene ID；
    # 场景标题含场次前缀，按 location（教室/走廊）映射到落库场景
    scene_id_by_location = {scene.title: scene.id for scene in scenes}
    for scene in structure["scenes"]:
        real_scene_id = scene_id_by_location[scene["location"]]
        scene_beats = [beat for beat in structure["beats"] if beat["scene_id"] == scene["scene_id"]]
        for position, beat in enumerate(scene_beats, start=1):
            plan_shots.append({
                "shot_id": f"sht_e2e_{real_scene_id[-6:]}_{position}",
                "scene_id": real_scene_id,
                "beat_ids": [beat["beat_id"]],
                "sequence": position,
                "duration": 4,
                "shot_kind": "visual",
                "visual_description": f"E2E 规划镜头 {position}",
                "source_block_ids": beat["source_block_ids"],
                "character_asset_ids": [character_id],
            })
    r = client.post(
        f"/api/projects/{pid}/script-versions/{sv['script_version_id']}/storyboard-plan/validate",
        json={"plan": {"plan_version": 1, "shots": plan_shots}},
    )
    assert r.status_code == 200, r.text
    preview = r.json()
    assert preview["valid"] is True, preview["issues"]
    assert preview["adapter"]["writable"] is False
    assert preview["comparison"]["existing_shot_count"] == 3

    db = _session()
    try:
        assert db.query(Shot).join(Scene, Shot.scene_id == Scene.id).filter(
            Scene.project_id == pid).count() == 3
    finally:
        db.close()

    # --- 5. 分镜图 ---
    first_shot = shot_ids[0]
    _execute(_generate(client, f"/api/shots/{first_shot}/generate",
                       {"target": "keyframe", "workflow_id": image_wf}))

    # --- 6. 视频（前置：分镜图就绪） ---
    video_wf = _workflow(client, "e2e-video", "video", _VIDEO_WF)
    result = _execute(_generate(client, f"/api/shots/{first_shot}/generate",
                                {"target": "clip", "workflow_id": video_wf}))
    clip_id = result["assets"][0]["asset_id"]

    db = _session()
    try:
        clip = db.get(Asset, clip_id)
        assert clip is not None
        assert clip.asset_type == "clip"
        assert clip.source_job_id
    finally:
        db.close()

    # --- 7. 只读影响预检：不写库、不改镜头 ---
    db = _session()
    try:
        from app.models import Job

        jobs_before = db.query(Job).filter(Job.project_id == pid).count()
    finally:
        db.close()

    for url in (
        f"/api/projects/{pid}/dependency-graph/impact-preview",
        f"/api/projects/{pid}/workflow-impact",
    ):
        response = client.get(url)
        assert response.status_code == 200, response.text

    db = _session()
    try:
        from app.models import Job

        assert db.query(Job).filter(Job.project_id == pid).count() == jobs_before
        after = (
            db.query(Shot)
            .join(Scene, Shot.scene_id == Scene.id)
            .filter(Scene.project_id == pid)
            .order_by(Shot.seq)
            .all()
        )
        assert [
            (shot.id, shot.seq, shot.duration, shot.dialogue, shot.image_prompt) for shot in after
        ] == shot_snapshot
    finally:
        db.close()


def test_generation_failure_then_retry_succeeds(client: TestClient, monkeypatch):
    """E2E 中途失败：mock 渲染抛错 → 任务 failed、无资产；显式重试后成功且只落一份资产。"""
    from app.models import Asset, Character, Job

    pid = client.post("/api/projects", json={"name": "失败重试 E2E", "aspect_ratio": "9:16"}).json()["id"]
    sv = client.post(f"/api/projects/{pid}/script-versions", json={"text": SCRIPT_TEXT}).json()
    _run_parse(client, pid, sv, monkeypatch)

    db = _session()
    try:
        character_id = db.query(Character).filter(Character.project_id == pid).one().id
    finally:
        db.close()

    image_wf = _workflow(client, "e2e-image", "image", _IMAGE_WF)

    from app.services import mock_render

    original = mock_render.mock_outputs
    calls = {"count": 0}

    def _flaky(output_map, job_id):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("模拟渲染失败")
        return original(output_map, job_id)

    monkeypatch.setattr(mock_render, "mock_outputs", _flaky)

    job_id = _generate(client, f"/api/characters/{character_id}/generate",
                       {"workflow_id": image_wf})
    from app.worker.executor import execute_generate_job

    failed = execute_generate_job(job_id)
    assert failed["ok"] is False
    assert failed["reason"] == "mock_render_failed"

    detail = client.get(f"/api/jobs/{job_id}").json()
    assert detail["status"] == "failed"
    assert detail["error_code"] == "mock_render_failed"
    assert "模拟渲染失败" in detail["error_msg"]
    assert detail["retryable"] is True

    db = _session()
    try:
        assert db.query(Asset).filter(Asset.source_job_id == job_id).count() == 0
    finally:
        db.close()

    r = client.post(f"/api/jobs/{job_id}/retry")
    assert r.status_code == 201, r.text
    retried = r.json()
    assert retried["id"] != job_id
    assert retried["status"] == "queued"
    assert retried["attempts"] == 1

    ok = execute_generate_job(retried["id"])
    assert ok["ok"] is True, ok

    db = _session()
    try:
        assets = db.query(Asset).filter(Asset.source_job_id == retried["id"]).all()
        assert len(assets) == 1
        assert assets[0].asset_type == "character"
        assert db.get(Job, job_id).status == "failed"
        assert db.get(Job, retried["id"]).status == "succeeded"
        assert db.get(Job, retried["id"]).attempts == 2
    finally:
        db.close()


def test_complex_multi_episode_pipeline(client: TestClient, monkeypatch):
    """E2E 样例 C：两集 / 三场景 / 双角色，场景批量分镜图 + 同集链式尾帧 + 跨集拦截。"""
    from app.models import Character, Job, Prop, Scene, Shot

    pid = client.post("/api/projects", json={"name": "复杂路径 E2E", "aspect_ratio": "9:16"}).json()["id"]
    sv = client.post(f"/api/projects/{pid}/script-versions", json={"text": SCRIPT_TEXT_COMPLEX}).json()
    _run_parse(client, pid, sv, monkeypatch, text=SCRIPT_TEXT_COMPLEX,
               payload=FAKE_PARSE_COMPLEX, outline=FAKE_OUTLINE_COMPLEX, episode_count=2)

    db = _session()
    try:
        characters = db.query(Character).filter(Character.project_id == pid).all()
        props = db.query(Prop).filter(Prop.project_id == pid).all()
        scenes = (
            db.query(Scene).filter(Scene.project_id == pid)
            .order_by(Scene.episode, Scene.seq).all()
        )
        shots = (
            db.query(Shot).join(Scene, Shot.scene_id == Scene.id)
            .filter(Scene.project_id == pid)
            .order_by(Shot.episode, Scene.seq, Shot.seq).all()
        )
        assert len(characters) == 2
        assert len(props) == 2
        assert [(scene.episode, scene.title) for scene in scenes] == [
            (1, "教室"), (1, "走廊"), (2, "屋顶"),
        ]
        assert len(shots) == 6
        classroom_id, corridor_id, roof_id = (scene.id for scene in scenes)
        classroom_shots = [shot.id for shot in shots if shot.scene_id == classroom_id]
        corridor_shots = [shot.id for shot in shots if shot.scene_id == corridor_id]
        roof_shots = [shot.id for shot in shots if shot.scene_id == roof_id]
        assert (len(classroom_shots), len(corridor_shots), len(roof_shots)) == (3, 1, 2)
        character_ids = [character.id for character in characters]
        prop_ids = [prop.id for prop in props]
    finally:
        db.close()

    # 公共资产：双角色定妆图、三场景基准图、双道具锚定图
    image_wf = _workflow(client, "e2e-image", "image", _IMAGE_WF)
    for character_id in character_ids:
        _execute(_generate(client, f"/api/characters/{character_id}/generate",
                           {"workflow_id": image_wf}))
    for scene_id in (classroom_id, corridor_id, roof_id):
        _execute(_generate(client, f"/api/scenes/{scene_id}/generate-baseline",
                           {"workflow_id": image_wf}))
    for prop_id in prop_ids:
        _execute(_generate(client, f"/api/props/{prop_id}/generate", {"workflow_id": image_wf}))

    db = _session()
    try:
        from app.models import Project

        project = db.get(Project, pid)
        project.assets_review_status = "approved"
        db.commit()
    finally:
        db.close()

    # 场景批量分镜图（教室 3 镜，满足批量端点 ≥3 的限制）
    r = client.post(f"/api/scenes/{classroom_id}/generate",
                    json={"target": "keyframe", "workflow_id": image_wf})
    assert r.status_code == 202, r.text
    batch = r.json()
    assert batch["count"] == 3
    for job in batch["jobs"]:
        _execute(job["id"])

    for shot_id in corridor_shots + roof_shots:
        _execute(_generate(client, f"/api/shots/{shot_id}/generate",
                           {"target": "keyframe", "workflow_id": image_wf}))

    # 同集链式尾帧：第 1 镜视频 → 尾帧 → 第 2 镜引用 → 第 3 镜引用第 2 镜
    video_wf = _workflow(client, "e2e-video", "video", _VIDEO_WF)
    first_job_id = _generate(client, f"/api/shots/{classroom_shots[0]}/generate",
                             {"target": "clip", "workflow_id": video_wf})
    _execute(first_job_id)
    db = _session()
    try:
        assert db.get(Shot, classroom_shots[0]).last_frame_asset_id
        # 集首镜不走同集尾帧注入，交由跨集连续性流程处理
        first_prev = db.get(Job, first_job_id).input_snapshot["prev_frame"]
        assert first_prev["injected"] is False
        assert first_prev["reason"] == "first_shot_handled_by_continuity"
    finally:
        db.close()

    second_job_id = _generate(client, f"/api/shots/{classroom_shots[1]}/generate",
                              {"target": "clip", "workflow_id": video_wf})
    _execute(second_job_id)
    third_job_id = _generate(client, f"/api/shots/{classroom_shots[2]}/generate",
                             {"target": "clip", "workflow_id": video_wf})
    _execute(third_job_id)

    db = _session()
    try:
        second = db.get(Job, second_job_id).input_snapshot["prev_frame"]
        assert second["injected"] is True
        assert second["prev_shot_id"] == classroom_shots[0]
        assert second["frame_asset_id"] == db.get(Shot, classroom_shots[0]).last_frame_asset_id

        third = db.get(Job, third_job_id).input_snapshot["prev_frame"]
        assert third["injected"] is True
        assert third["prev_shot_id"] == classroom_shots[1]
        assert third["frame_asset_id"] == db.get(Shot, classroom_shots[1]).last_frame_asset_id
    finally:
        db.close()

    # 跨集不串帧：第 1 集未合成、无连续性记录时，第 2 集首镜被显式拦截（422）
    db = _session()
    try:
        jobs_before = db.query(Job).count()
    finally:
        db.close()
    blocked = client.post(f"/api/shots/{roof_shots[0]}/generate",
                          json={"target": "clip", "workflow_id": video_wf})
    assert blocked.status_code == 422, blocked.text
    assert blocked.json()["detail"]["code"] == "continuity_check_failed"
    db = _session()
    try:
        assert db.query(Job).count() == jobs_before
        assert db.get(Shot, roof_shots[0]).last_frame_asset_id is None
    finally:
        db.close()

    # 只读影响预检仍可用，且不改变镜头
    db = _session()
    try:
        shot_count = db.query(Shot).join(Scene, Shot.scene_id == Scene.id).filter(
            Scene.project_id == pid).count()
    finally:
        db.close()
    for url in (
        f"/api/projects/{pid}/dependency-graph/impact-preview",
        f"/api/projects/{pid}/workflow-impact",
    ):
        assert client.get(url).status_code == 200
    db = _session()
    try:
        assert db.query(Shot).join(Scene, Shot.scene_id == Scene.id).filter(
            Scene.project_id == pid).count() == shot_count
    finally:
        db.close()
