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
