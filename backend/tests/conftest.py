"""全局测试夹具。

P1-1 修复（FINAL_CODE_AUDIT_REPORT.md）：提交 68cd8de 在创建项目前引入
_check_runtime_config 运行时配置校验（LLM/ComfyUI 未配置返回 409），
但测试环境不配置外部服务，导致 37 个集成测试在创建项目第一步即失败。

该校验属于环境前置条件（引导用户先到设置页配置），不是被测业务逻辑；
测试中统一绕过，由独立的配置校验测试覆盖其行为。
"""
import pytest


@pytest.fixture(autouse=True)
def _bypass_runtime_config_check(monkeypatch):
    """所有测试中跳过创建项目的 LLM/ComfyUI 配置校验（外部服务依赖）。"""
    import app.api.v1.projects as projects_mod
    monkeypatch.setattr(projects_mod, "_check_runtime_config", lambda: None)


def seed_scene_baseline_asset(db, scene, media_dir) -> str:
    """为场景构造真实基准图资产（D-23 资产就绪门禁通过所需）。

    门禁校验的是资产真实状态：指针（baseline_asset_id）→ 有效性（current）
    → 文件存在（media_dir 下真实文件）。只设置审核标志不再足够。
    返回资产 ID。
    """
    from datetime import datetime
    from pathlib import Path
    from app.models import Asset
    from app.models.ids import asset_id
    media = Path(media_dir)
    media.mkdir(parents=True, exist_ok=True)
    fname = f"baseline_{asset_id()}.png"
    fpath = media / fname
    fpath.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)  # 最小 PNG 头占位
    a = Asset(
        id=asset_id(),
        project_id=scene.project_id,
        asset_type="scene",
        name=f"{scene.title}-基准图",
        version=1,
        validity="current",
        file_path=str(fpath),
    )
    db.add(a)
    db.flush()
    scene.baseline_asset_id = a.id
    db.commit()
    return a.id


def approve_project_assets(db, project_id) -> None:
    """放行公共资产人工审核（D-23 审核闸口），供聚焦非审核逻辑的测试使用。"""
    from app.models import Project
    p = db.get(Project, project_id)
    if p is not None:
        p.assets_review_status = "approved"
        db.commit()
