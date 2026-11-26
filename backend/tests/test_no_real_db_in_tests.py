"""守卫：tests/ 下不得出现真实库路径字面量。

背景：历史上多个测试在模块顶层 `create_engine("sqlite:///./data/director_lab.db")`，
pytest 一收集就写真实库。这里做静态扫描，防止回退。
"""
from __future__ import annotations

from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SELF = Path(__file__).resolve()
FORBIDDEN = "data/" + "director_lab.db"


def test_no_real_database_literal_in_tests():
    offenders = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if path.resolve() == SELF:
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        if FORBIDDEN in text:
            offenders.append(str(path.relative_to(TESTS_DIR)))
    assert not offenders, "以下测试文件仍引用真实库路径：" + ", ".join(offenders)


def test_no_module_level_engine_on_real_path():
    """模块顶层（无缩进）直接 create_engine 必须走 tmp_path，禁止连接磁盘上的库。

    函数内的 create_engine(db_url) 不受限制：那是由 fixture 传入的 tmp 地址。
    """
    offenders = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if path.resolve() == SELF:
            continue
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1
        ):
            if line[:1] in {" ", "\t"}:
                continue
            stripped = line.strip()
            if stripped.startswith(("create_engine(", "engine = create_engine(")):
                if "tmp_path" not in stripped and "make_engine" not in stripped:
                    offenders.append(f"{path.name}:{lineno}")
    assert not offenders, "模块级 create_engine 必须走 tmp_path：" + ", ".join(offenders)
