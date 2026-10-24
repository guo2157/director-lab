"""提示词外置服务（D-15）：LLM 提示词从 workspace/prompts/*.md 加载，UI 可编辑。

设计约定：
- 提示词文件位于 backend/workspace/prompts/，文件名即提示词名（如 parse_system.md）
- 文件缺失时回退到代码内置默认值（DEFAULT_PROMPTS），保证零配置可用
- 写入时保留 .bak 历史版本（文件级备份，最多 5 份），便于回滚
- 读取即时生效：每次调用 get_prompt 都重新读文件，无需重启
- 按模型覆盖（远期）：同名 + 模型后缀文件优先（如 parse_system.gpt-4o-mini.md）
"""
import re
import shutil
from pathlib import Path

from app.core.config import get_settings

# 提示词目录：backend/workspace/prompts
PROMPT_DIR = Path(__file__).resolve().parents[1] / "workspace" / "prompts"

# 内置默认提示词（与 workspace 文件缺失时的回退值保持一致）
DEFAULT_PROMPTS: dict[str, str] = {}


def _register_default(name: str, content: str) -> str:
    """注册内置默认提示词（由定义模块在 import 时调用）。"""
    DEFAULT_PROMPTS[name] = content
    return content


def list_prompts() -> list[dict]:
    """列出全部提示词：名称、是否已外置、内容哈希前 8 位。"""
    import hashlib

    result = []
    names = set(DEFAULT_PROMPTS)
    if PROMPT_DIR.exists():
        for f in PROMPT_DIR.glob("*.md"):
            names.add(f.stem)
    for name in sorted(names):
        content = get_prompt(name)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:8]
        result.append({
            "name": name,
            "externalized": (PROMPT_DIR / f"{name}.md").exists(),
            "sha256_8": digest,
            "length": len(content),
        })
    return result


def get_prompt(name: str) -> str:
    """读取提示词：外置文件优先，缺失回退内置默认值。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError(f"非法提示词名: {name}")
    path = PROMPT_DIR / f"{name}.md"
    if path.exists():
        return path.read_text(encoding="utf-8")
    if name in DEFAULT_PROMPTS:
        return DEFAULT_PROMPTS[name]
    raise FileNotFoundError(f"提示词不存在: {name}")


def write_prompt(name: str, content: str) -> dict:
    """写入提示词文件（创建或覆盖），写入前备份当前版本。"""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError(f"非法提示词名: {name}")
    if name not in DEFAULT_PROMPTS:
        raise FileNotFoundError(f"提示词不存在: {name}")
    PROMPT_DIR.mkdir(parents=True, exist_ok=True)
    path = PROMPT_DIR / f"{name}.md"
    # 备份当前版本（最多保留 5 份）
    if path.exists():
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d%H%M%S")
        bak = PROMPT_DIR / f"{name}.{ts}.bak"
        shutil.copy2(path, bak)
        baks = sorted(PROMPT_DIR.glob(f"{name}.*.bak"))
        for old in baks[:-5]:
            old.unlink(missing_ok=True)
    path.write_text(content, encoding="utf-8")
    return {"name": name, "written": True, "length": len(content)}


def reset_prompt(name: str) -> dict:
    """重置提示词为内置默认值（删除外置文件）。"""
    if name not in DEFAULT_PROMPTS:
        raise FileNotFoundError(f"提示词不存在: {name}")
    path = PROMPT_DIR / f"{name}.md"
    if path.exists():
        path.unlink()
    return {"name": name, "reset": True}
