"""内置默认模板与外置模板一致性防漂移测试（P2-2）。

约束：外置模板（workspace/prompts/*.md）与内置默认（DEFAULT_PROMPTS）必须逐字节一致
（允许末尾换行符差异）。任何一侧改动未同步，本测试即失败，避免 P0 修复过的
模板漂移问题复发。

设计约定：
- 只读比较，绝不写入或删除 workspace/prompts 下的真实文件
- 覆盖全部注册模板（含 P1-1 新注册的 9 个内联提示词）
- 漂移时输出具体模板名与 diff 摘要，便于定位哪一侧是旧版
"""
import difflib

import pytest

from app.services import prompt_store


def _normalize(content: str) -> str:
    """归一化：去除末尾换行符差异（内置默认无尾换行，外置文件通常有）。"""
    return content.rstrip("\n")


def _diff_summary(builtin: str, external: str, max_lines: int = 10) -> str:
    """生成内置/外置差异摘要（前 max_lines 行），供断言失败时定位漂移方向。"""
    diff = list(
        difflib.unified_diff(
            builtin.splitlines(),
            external.splitlines(),
            fromfile="builtin",
            tofile="external",
            lineterm="",
            n=1,
        )
    )
    return "\n".join(diff[:max_lines]) if diff else ""


def test_all_externalized_prompts_match_builtin():
    """全部已外置模板必须与内置默认逐字节一致（归一化后）。"""
    prompt_store.ensure_registered()
    mismatched: list[str] = []
    for name in sorted(prompt_store.DEFAULT_PROMPTS):
        ext_path = prompt_store.PROMPT_DIR / f"{name}.md"
        if not ext_path.exists():
            continue
        builtin = _normalize(prompt_store.DEFAULT_PROMPTS[name])
        external = _normalize(ext_path.read_text(encoding="utf-8"))
        if builtin != external:
            summary = _diff_summary(builtin, external)
            mismatched.append(f"{name}:\n{summary}")
    assert not mismatched, (
        f"{len(mismatched)} 个外置模板与内置默认不一致（漂移复发）：\n"
        + "\n\n".join(mismatched)
    )


def test_ensure_registered_idempotent_and_complete():
    """ensure_registered 幂等且覆盖全部注册模块，注册数不随重复调用变化。"""
    prompt_store.ensure_registered()
    first_count = len(prompt_store.DEFAULT_PROMPTS)
    prompt_store.ensure_registered()
    prompt_store.ensure_registered()
    assert len(prompt_store.DEFAULT_PROMPTS) == first_count
    assert first_count >= 27, f"注册模板数 {first_count} 少于预期 27（P0+P1 基线）"


def test_list_prompts_covers_all_registered():
    """list_prompts 必须覆盖全部已注册模板，无遗漏。"""
    items = prompt_store.list_prompts()
    listed_names = {item["name"] for item in items}
    prompt_store.ensure_registered()
    registered_names = set(prompt_store.DEFAULT_PROMPTS)
    missing = registered_names - listed_names
    assert not missing, f"list_prompts 遗漏注册模板：{sorted(missing)}"
