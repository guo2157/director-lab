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
# 数据库隔离（2026-11-26 真实事故回归）：
# alembic/env.py 无条件用 get_settings().database_url 覆盖 Config 里的地址，
# 迁移测试即使传了临时库 URL，command.upgrade() 仍会连到真实库并写入 schema。
# 因此在导入任何 app 模块之前，先把数据库地址钉死在临时目录；
# 下面的 _guard_real_database 会话级夹具再做一次硬断言，任何一次会话都不允许指向真实库。
_TEST_DB_PATH = _TEST_DATA_ROOT / "data" / "director_lab.db"
_TEST_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
os.environ["DLAB_DATABASE_URL"] = f"sqlite:///{_TEST_DB_PATH}"
_BACKEND_DIR = Path(__file__).resolve().parents[1]
_REAL_DB_PATH = (_BACKEND_DIR / "data" / "director_lab.db").resolve()
os.environ["DLAB_RENDER_MOCK"] = "false"  # 避免 .env 的真实配置漏进测试
# API lifespan 会内嵌调度线程；测试不允许后台消费任务（会抢跑用例手工构造的 queued 任务）
os.environ["DLAB_WORKER_EMBEDDED"] = "false"


@pytest.fixture(scope="session", autouse=True)
def _guard_real_database():
    """整个测试会话禁止连接真实库（backend/data/director_lab.db）。

    alembic/env.py 用 get_settings().database_url 覆盖 Config 中的地址，
    单靠测试里传 URL 无效，所以这里在会话开始时校验解析后的实际地址。
    """
    from sqlalchemy.engine import make_url

    from app.core.config import clear_settings_cache, get_settings

    clear_settings_cache()
    url = make_url(get_settings().database_url)
    assert url.get_backend_name() == "sqlite", f"测试库必须是 SQLite，实际 {url}"
    db_path = Path(url.database or "").resolve()
    if db_path == _REAL_DB_PATH:
        raise RuntimeError(
            "测试会话解析到了真实库 backend/data/director_lab.db，已中止："
            "请检查 DLAB_DATABASE_URL 是否被 .env 或代码覆盖"
        )
    if _TEST_DATA_ROOT.resolve() not in db_path.parents:
        raise RuntimeError(f"测试库不在临时目录 {_TEST_DATA_ROOT} 下，已中止：{db_path}")

    # 模块级 engine 在 import 时按环境变量创建，必须同样是临时库，
    # 否则 app.core.database.SessionLocal 会把用例写进真实库。
    from app.core import database as database_mod

    engine_path = Path(database_mod.engine.url.database or "").resolve()
    if engine_path == _REAL_DB_PATH or _TEST_DATA_ROOT.resolve() not in engine_path.parents:
        raise RuntimeError(f"模块级 engine 未指向临时库，已中止：{engine_path}")
    yield


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
