"""提示词注册表一致性测试（P2-1）。

约束：PROMPT_REGISTRY 必须与 prompt_store.DEFAULT_PROMPTS 双向一致——
新增注册模板必须登记元数据，注册表条目也必须有对应模板，避免注册表悄悄腐化。
"""
import pytest

from app.services import prompt_registry as pr
from app.services import prompt_store


def test_registry_covers_all_registered_prompts():
    """全部注册模板必须登记在注册表中。"""
    prompt_store.ensure_registered()
    registered = set(prompt_store.DEFAULT_PROMPTS)
    registry = set(pr.PROMPT_REGISTRY)
    missing = registered - registry
    assert not missing, f"以下注册模板缺少注册表元数据：{sorted(missing)}"


def test_registry_has_no_orphan_entries():
    """注册表不得包含未注册的模板（孤儿条目）。"""
    prompt_store.ensure_registered()
    registered = set(prompt_store.DEFAULT_PROMPTS)
    orphans = set(pr.PROMPT_REGISTRY) - registered
    assert not orphans, f"注册表存在未注册模板：{sorted(orphans)}"


def test_registry_metadata_complete():
    """每条元数据的必填字段完整且 content_hash 非空。"""
    assert not pr.validate_registry(), pr.validate_registry()
    for prompt_id in pr.PROMPT_REGISTRY:
        meta = pr.template_meta(prompt_id)
        assert meta is not None
        assert meta["task_type"], f"{prompt_id} 缺少 task_type"
        assert meta["version"], f"{prompt_id} 缺少 version"
        assert meta["output_format"] in {"text", "json"}
        assert meta["model_compatibility"], f"{prompt_id} 缺少 model_compatibility"
        assert meta["content_hash"], f"{prompt_id} content_hash 为空"
        assert meta["template_path"].endswith(f"{prompt_id}.md")


def test_template_input_vars_match_actual_placeholders():
    """input_vars 必须与模板实际占位符一致（防止注册表与模板漂移）。"""
    import string

    for prompt_id in pr.PROMPT_REGISTRY:
        content = prompt_store.get_prompt(prompt_id)
        actual: list[str] = []
        for _, field_name, _, _ in string.Formatter().parse(content):
            if field_name and field_name not in actual:
                actual.append(field_name)
        assert pr.template_input_vars(prompt_id) == actual, (
            f"{prompt_id} 的 input_vars 与模板实际占位符不一致"
        )


def test_content_hash_tracks_template_change():
    """content_hash 必须随模板内容变化（外置优先读取，即时反映）。"""
    prompt_id = "session_title_system"
    before = pr.template_content_hash(prompt_id)
    assert before, "指纹为空，无法验证"
    original = prompt_store.get_prompt(prompt_id)
    try:
        prompt_store.write_prompt(prompt_id, original + "\n【临时变更】")
        after = pr.template_content_hash(prompt_id)
        assert after != before, "模板变更后 content_hash 未变化，追溯链路失效"
    finally:
        prompt_store.write_prompt(prompt_id, original)
    assert pr.template_content_hash(prompt_id) == before, "回滚后指纹未复原"


def test_registry_snapshot_shape():
    """快照包含全部注册表条目且标记注册与外置状态。"""
    rows = pr.registry_snapshot()
    assert len(rows) == len(pr.PROMPT_REGISTRY)
    for row in rows:
        assert row["registered"] is True
        assert isinstance(row["externalized"], bool)
