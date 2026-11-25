"""全局测试夹具。

P1-1 修复（FINAL_CODE_AUDIT_REPORT.md）：提交 68cd8de 在创建项目前引入
_check_runtime_config 运行时配置校验（LLM/ComfyUI 未配置返回 409），
但测试环境不配置外部服务，导致 37 个集成测试在创建项目第一步即失败。

该校验属于环境前置条件（引导用户先到设置页配置），不是被测业务逻辑；
测试中统一绕过，由独立的配置校验测试覆盖其行为。
"""
import os
import tempfile
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# 测试隔离（2026-11-21 真实事故回归）：
# Settings 的 data_dir/media_dir 默认值是真实路径 ``./data``/``./data/media``。
# 大量用例只 monkeypatch ``get_settings`` 的 render_mock，未指定 media_dir，
# 于是 render_mock 产物会写进 backend/data/media —— executor._store_shot_keyframe
# 的命名是 ``storyboard_{项目内全局镜号}_img.png``，测试库里新建项目的第 1 镜恰好
# 也是 1，直接覆盖了线上第一镜的分镜图（表现为镜头详情里「图1 ref_image」黑屏）。
# 这里用环境变量把所有未显式指定目录的 Settings 实例重定向到临时目录。
# 显式传 media_dir=tmp_path 的用例不受影响。
_TEST_DATA_ROOT = Path(tempfile.mkdtemp(prefix="dlab-test-media-"))
os.environ.setdefault("DLAB_DATA_DIR", str(_TEST_DATA_ROOT / "data"))
os.environ.setdefault("DLAB_MEDIA_DIR", str(_TEST_DATA_ROOT / "data" / "media"))
os.environ["DLAB_RENDER_MOCK"] = "false"  # 避免 .env 的真实配置漏进测试
# API lifespan 会内嵌调度线程；测试不允许后台消费任务（会抢跑用例手工构造的 queued 任务）
os.environ["DLAB_WORKER_EMBEDDED"] = "false"


@pytest.fixture(autouse=True)
def _bypass_runtime_config_check(monkeypatch):
    """所有测试中跳过创建项目的 LLM/ComfyUI 配置校验（外部服务依赖）。"""
    import app.api.v1.projects as projects_mod
    monkeypatch.setattr(projects_mod, "_check_runtime_config", lambda: None)


@pytest.fixture(autouse=True)
def _block_real_vision_qc(monkeypatch):
    """测试套件内禁止发起真实多模态评图请求。

    背景（2026-11-25 实测）：走过 executor 关键帧成功路径的用例会触发
    ``apply_keyframe_image_qc``，它用 ``app.core.config.get_settings()`` 读真实
    配置（本文件强制 DLAB_RENDER_MOCK=false），于是把 mock 渲染出的占位图
    送去真实 vision 接口。表现是用例不报错但挂起 100s 以上直到超时，
    曾导致整组回归卡死、只能 SIGINT 中断。

    这里只把 ``keyframe_image_qc`` 模块内的 get_settings 指向 render_mock=True，
    让 ``_call_vision_qc`` 走既有守卫直接返回 None（降级为 vision_qc_unavailable
    告警）。需要校验真实 vision 逻辑的用例自带 monkeypatch，会覆盖本夹具。
    """
    import app.services.keyframe_image_qc as qc_mod
    from app.core.config import Settings

    monkeypatch.setattr(qc_mod, "get_settings", lambda: Settings(render_mock=True))


@pytest.fixture(autouse=True)
def _block_real_scene_plate_llm(monkeypatch):
    """测试套件内禁止场景板编译发起真实 LLM 请求。

    背景（2026-11-25 实测）：``build_asset_plans`` 会为每个场景调用
    ``compile_scene_plate``，后者命中不到 plate_text 缓存时直接
    ``LLMClient().chat_json``。测试库里的场景都是新建的、没有 plate_text，
    于是每个用例都要等真实 LLM 往返 + 失败重试，单个用例耗时约 37 秒，
    4 个槽位用例累计 110 秒以上，表现为「回归卡住」。

    这里让 ``_call_llm`` 直接抛 LLMError，走既有的规则兜底分支
    （persist_fallback=False → source=rules_transient），与「LLM 未配置」
    的生产行为一致。专门校验场景板 LLM 行为的用例（test_asset_prompt_hardening
    / test_pipeline_e2e）自己会 monkeypatch ``_call_llm``，覆盖本夹具。
    """
    import app.services.scene_plate as plate_mod
    from app.integrations.llm import LLMError

    def _no_llm(*args, **kwargs):
        raise LLMError("llm_unconfigured", "测试环境不发起真实场景板 LLM 请求")

    monkeypatch.setattr(plate_mod, "_call_llm", _no_llm)


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
