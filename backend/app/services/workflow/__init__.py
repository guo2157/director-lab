"""动态工作流服务（P1）：Schema 构建与静态校验。

设计约定（对应 docs/plan/workflow-dynamic-audit.md P1）：
- 复用 services/workflow_inject.parse_workflow 的标记解析，不另建一套标记规则
- parsed_schema 统一为「参数字典 + 输出数组」形状，与 scripts/seed_workflows.py 写入的形状一致
- binding（node_id / input_key）只留在后端；对外 Schema 接口会剥离
"""

from app.services.workflow.schema_builder import (  # noqa: F401
    PARSER_VERSION,
    SCHEMA_VERSION,
    build_schema,
    public_schema,
)
from app.services.workflow.validator import validate_definition  # noqa: F401

__all__ = [
    "PARSER_VERSION",
    "SCHEMA_VERSION",
    "build_schema",
    "public_schema",
    "validate_definition",
]
