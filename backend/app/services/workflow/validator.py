"""工作流静态校验（P1）：上传/校验共用，不访问 ComfyUI。

与 backend/scripts/validate_workflows.py 同套规则，但：
- 返回结构化 issue（code / node_id / field / message），前端可定位到节点与字段
- 增加上传安全上限：节点数、参数数、节点 ID 合法性、连线目标存在性
- 显式拒绝画布格式（nodes + links），提示改用 API Format 导出
"""

import re

from app.services.workflow_inject import CANVAS_ONLY_KEYS, parse_workflow

MAX_NODES = 400
MAX_PARAMS = 64
MAX_NODE_ID_LEN = 16

_NODE_ID_RE = re.compile(r"^\d{1,6}$")

CANVAS_FORMAT_HINT = (
    "当前上传的文件不是 ComfyUI API Format JSON。"
    "请在 ComfyUI 中导出 API 格式工作流后重新上传。"
)

# trigger.target → 必需输出标记（WORKFLOW.md §4）
_REQUIRED_OUTPUTS = {
    "keyframe": ("cover",),
    "clip": ("clip",),
    "audio": ("voice",),
    "bgm": ("bgm",),
    "sfx": ("sfx",),
}


def _issue(code: str, message: str, *, node_id: str | None = None,
           field: str | None = None) -> dict:
    out = {"code": code, "message": message}
    if node_id is not None:
        out["node_id"] = node_id
    if field is not None:
        out["field"] = field
    return out


def validate_definition(definition: object, *, trigger: dict | None = None) -> dict:
    """返回 {valid, errors:[{code,message,node_id?,field?}], warnings:[str]}。"""
    errors: list[dict] = []
    warnings: list[str] = []

    if not isinstance(definition, dict):
        return {"valid": False,
                "errors": [_issue("workflow_format_invalid", "工作流定义必须是 JSON 对象")],
                "warnings": []}

    # 画布格式：顶层 nodes + links
    if isinstance(definition.get("nodes"), list) and "links" in definition:
        return {"valid": False,
                "errors": [_issue("workflow_format_invalid", CANVAS_FORMAT_HINT)],
                "warnings": []}

    if not definition:
        return {"valid": False,
                "errors": [_issue("workflow_format_invalid", "工作流定义为空")],
                "warnings": []}

    if len(definition) > MAX_NODES:
        errors.append(_issue(
            "workflow_too_large",
            f"节点数 {len(definition)} 超过上限 {MAX_NODES}",
        ))

    for node_id, node in definition.items():
        if not isinstance(node_id, str) or not _NODE_ID_RE.match(node_id) or len(node_id) > MAX_NODE_ID_LEN:
            errors.append(_issue(
                "workflow_node_id_invalid",
                f"节点 ID 非法：{node_id!r}（API Format 的节点 ID 必须是数字字符串）",
                node_id=str(node_id),
            ))
            continue
        if not isinstance(node, dict):
            errors.append(_issue("workflow_format_invalid", "节点定义必须是对象", node_id=node_id))
            continue
        class_type = node.get("class_type")
        inputs = node.get("inputs")
        if not isinstance(class_type, str) or not class_type:
            errors.append(_issue("workflow_format_invalid", "节点缺少 class_type", node_id=node_id))
        if not isinstance(inputs, dict):
            errors.append(_issue("workflow_format_invalid", "节点缺少 inputs 对象", node_id=node_id))
            continue
        for key, value in inputs.items():
            if key in CANVAS_ONLY_KEYS:
                errors.append(_issue(
                    "workflow_canvas_residue",
                    f"含画布专用残留键 {key}，须剔除后重新导出（WORKFLOW.md §1）",
                    node_id=node_id, field=key,
                ))
            # 连线：[源节点ID, 输出序号]；源节点必须存在
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                if value[0] not in definition:
                    errors.append(_issue(
                        "workflow_connection_broken",
                        f"连线指向不存在的节点 {value[0]}",
                        node_id=node_id, field=key,
                    ))

    parsed = parse_workflow(definition)
    for msg in parsed.errors:
        errors.append(_issue("workflow_marker_missing", msg))
    warnings.extend(parsed.warnings)

    if len(parsed.params) > MAX_PARAMS:
        errors.append(_issue(
            "workflow_too_large",
            f"可配置参数 {len(parsed.params)} 个，超过上限 {MAX_PARAMS}（参数应按需暴露，不应对全节点开放）",
        ))

    # 宽高必须成对（WORKFLOW.md §5：单侧绑定只告警，不静默丢弃）
    if ("width" in parsed.params) != ("height" in parsed.params):
        warnings.append("width / height 仅绑定一侧：该侧以工作流内置值为准")

    required = _REQUIRED_OUTPUTS.get(str((trigger or {}).get("target") or ""), ())
    for name in required:
        if name not in parsed.outputs:
            errors.append(_issue(
                "workflow_marker_missing",
                f"缺少必需输出标记 $output.{name}（target={(trigger or {}).get('target')}）",
            ))

    return {"valid": not errors, "errors": errors, "warnings": warnings}


def issue_messages(result: dict) -> list[str]:
    """扁平化为前端可展示的字符串数组（兼容旧 validate 响应的 issues 字段）。"""
    return [str(i.get("message") or i.get("code") or "") for i in (result.get("errors") or [])]
