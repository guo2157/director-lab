"""P5：full_reconfigure 重新生成必须带上 workflow_id 与 params（端到端补测）。

前端「切换工作流 / 改参数」这一档的后端契约此前没有测试覆盖：
- 只有 full_reconfigure 允许更换工作流（其余模式返回 422，避免静默改历史配置）
- 提交后 job 必须固定新工作流及其版本，参数进入快照
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.main import app
from app.core.database import make_engine
from app.models import Asset, Base, Job, Workflow
from app.services import regeneration as regen


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(db_mod, "SessionLocal",
                        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False))
    return TestClient(app)


def _seed(client, tmp_path: Path) -> tuple[str, str, str]:
    """项目/场景/镜头 + 一个成功的基础分镜图任务与资产 + 两个图片工作流。"""
    import app.core.database as db_mod

    pid = client.post("/api/projects", json={"name": "全量重配测试", "aspect_ratio": "9:16"}).json()["id"]
    scn = client.post("/api/scenes", json={"project_id": pid, "title": "站台", "episode": 1}).json()
    shot = client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": 1, "shot_kind": "visual", "duration": 4,
    }).json()

    base_file = tmp_path / "base_keyframe.png"
    base_file.write_bytes(b"fake-image-bytes")

    db = db_mod.SessionLocal()
    try:
        def _wf(wf_id: str, version: int, width: int) -> Workflow:
            return Workflow(
                id=wf_id, name=f"图片工作流-{wf_id}", workflow_type="image",
                definition={
                    "1": {"class_type": "TextEncodeQwenImage21",
                          "_meta": {"title": "文本 $prompt.prompt!"},
                          "inputs": {"prompt": "占位"}},
                    "2": {"class_type": "EmptyLatentImage",
                          "_meta": {"title": "画布 $width, $height"},
                          "inputs": {"width": width, "height": 1024}},
                    "3": {"class_type": "SaveImage",
                          "_meta": {"title": "$output.cover"}, "inputs": {}},
                },
                capabilities={"reference_slots": []},
                version=version, is_builtin=True,
            )

        db.add(_wf("wf_old_image", 3, 1024))
        db.add(_wf("wf_new_image", 5, 768))
        db.flush()

        base_job = Job(
            id="job_kf_base01", project_id=pid, shot_id=shot["id"], job_type="generate_keyframe",
            workflow_id="wf_old_image", workflow_version=3,
            params={"prompt": "原提示词"}, status="succeeded", created_at=regen._now(),
            input_snapshot={"params": {"prompt": "原提示词"}},
        )
        db.add(base_job)
        db.flush()
        db.add(Asset(
            id="ast_kf_base01", project_id=pid, asset_type="keyframe", name="镜1分镜图",
            version=1, file_path=str(base_file), source_job_id=base_job.id,
            validity="current",
        ))
        db.commit()
    finally:
        db.close()
    return pid, shot["id"], "wf_new_image"


def test_full_reconfigure_switches_workflow_and_records_params(client: TestClient, tmp_path):
    _pid, shot_id, new_wf = _seed(client, tmp_path)
    resp = client.post(f"/api/shots/{shot_id}/regenerate/workbench", json={
        "target": "keyframe",
        "mode": "full_reconfigure",
        "prompt": "换工作流后的新提示词",
        "workflow_id": new_wf,
        "params": {"prompt": "换工作流后的新提示词", "width": 768},
    })
    assert resp.status_code == 202, resp.text
    job = resp.json()
    assert job["workflow_id"] == new_wf
    assert job["workflow_version"] == 5

    import app.core.database as db_mod
    db = db_mod.SessionLocal()
    try:
        row = db.get(Job, job["id"])
        # 只传 width 不传 height 时，画布参数**故意**被剔除：
        # 「图片画布由执行器按当前项目画幅重算」，避免 16:9 项目继续沿用
        # 历史 768×1344 竖图画布（见 regeneration.py 的 pop width/height 分支）。
        # 旧断言要求 width==768，与这一设计矛盾，属过期断言。
        assert "width" not in (row.params or {})
        assert "height" not in (row.params or {})
        assert (row.params or {}).get("prompt") == "换工作流后的新提示词"
        assert (row.input_snapshot or {}).get("workflow_version") == 5
    finally:
        db.close()


def test_switching_workflow_is_rejected_outside_full_reconfigure(client: TestClient, tmp_path):
    _pid, shot_id, new_wf = _seed(client, tmp_path)
    resp = client.post(f"/api/shots/{shot_id}/regenerate/workbench", json={
        "target": "keyframe",
        "mode": "inherit_prompt_only",
        "workflow_id": new_wf,
    })
    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "invalid_request"
