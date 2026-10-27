"""工作流标记解析与参数注入引擎：WORKFLOW.md §2 标记语法、§3 统一参数约定、§5 参数对齐。

与 backend/scripts/validate_workflows.py 的解析逻辑保持一致（同一套标记规则）。
标记写在节点 _meta.title，不写 inputs；注入时深拷贝工作流定义，不改原定义。
"""
import copy
import re
from dataclasses import dataclass, field


@dataclass
class ParamBinding:
    """一个参数绑定：节点 ID、目标字段、是否媒体、是否必填。"""
    node_id: str
    field: str
    media: bool = False
    required: bool = False


@dataclass
class ParsedWorkflow:
    """解析结果：参数名→绑定、输出名集合、错误与警告。"""
    params: dict = field(default_factory=dict)   # name -> ParamBinding
    outputs: set = field(default_factory=set)
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


_MARK_RE = re.compile(r"\$(~?)([A-Za-z0-9_]+)(?:\.(~?)([A-Za-z0-9_.]+))?(!?)")

# 画布专用残留键（WORKFLOW.md §1 防复发规则）
CANVAS_ONLY_KEYS = {"control_after_generate", "widgets_values"}


def parse_workflow(wf: dict) -> ParsedWorkflow:
    """解析 API Format 工作流定义中的标记。"""
    result = ParsedWorkflow()
    if "nodes" in wf and "links" in wf:
        result.errors.append("画布格式，须为 API Format")
        return result
    for nid, node in wf.items():
        if not isinstance(node, dict) or "class_type" not in node or "inputs" not in node:
            result.errors.append(f"节点 {nid} 缺 class_type/inputs")
            continue
        # 画布残留键检测
        for k in node["inputs"]:
            if k in CANVAS_ONLY_KEYS:
                result.errors.append(f"节点 {nid} 含画布专用残留键 {k}")
        title = node.get("_meta", {}).get("title", "")
        for frag in [f.strip() for f in title.split(",")]:
            if not frag.startswith("$"):
                continue
            if frag.startswith("$output."):
                out_name = frag[8:]
                if out_name in result.outputs:
                    result.errors.append(f"节点 {nid} 输出 $output.{out_name} 重复标记")
                result.outputs.add(out_name)
                continue
            m = _MARK_RE.fullmatch(frag)
            if not m:
                result.errors.append(f"节点 {nid} 标记非法 {frag}")
                continue
            media = bool(m.group(1) or m.group(3))
            name = m.group(2).lower()
            field_name = m.group(4) or name
            required = bool(m.group(5))
            if field_name not in node["inputs"]:
                result.errors.append(f"节点 {nid} 标记 ${name} 指向不存在字段 {field_name}")
            if isinstance(node["inputs"].get(field_name), list):
                result.errors.append(f"节点 {nid} 字段 {field_name} 是连线，无法注入")
            if name in result.params:
                result.errors.append(f"参数 {name} 重复绑定")
            result.params[name] = ParamBinding(node_id=nid, field=field_name, media=media, required=required)
    if not result.outputs:
        result.errors.append("缺少 $output.* 输出标记 (workflow_marker_missing)")
    return result


def inject_params(wf: dict, values: dict, seed: int | None = None) -> tuple[dict, dict]:
    """向工作流深拷贝注入参数值。返回 (注入后的工作流, 实际注入快照)。

    - seed 为 random 策略下同一次 job 内所有种子项共用的值（WORKFLOW.md §5 种子规则）
    - 媒体参数的值应为上传后的 ComfyUI 文件名（上传在调用方完成）
    - 未提供的可选参数不注入，保持工作流内置值
    """
    parsed = parse_workflow(wf)
    if parsed.errors:
        raise ValueError("工作流标记校验失败: " + "; ".join(parsed.errors))
    out = copy.deepcopy(wf)
    snapshot = {}
    for name, binding in parsed.params.items():
        if name == "seed" and seed is not None:
            value = seed
        elif name in values and values[name] is not None:
            value = values[name]
        else:
            if binding.required:
                raise ValueError(f"必填参数 {name} 缺失")
            continue
        out[binding.node_id]["inputs"][binding.field] = value
        snapshot[name] = value
    return out, snapshot


def prune_unbound_media_inputs(wf: dict, values: dict) -> tuple[dict, list[str]]:
    """剔除未提供媒体的参考图节点及其连线（P4 整改）。

    工作流定义中的 LoadImage 节点带有占位图（placeholder_*.png）。
    此前未注入槽位时占位图连线被原样提交，占位图会污染生成结果
    （资产图混入陌生主体/无关元素的根因之一）。
    现按工作流 meta 约定：未注入的参考图槽位在提交前剔除 image 连线。

    返回 (剔除后的工作流, 被剔除的参数名列表)。
    """
    parsed = parse_workflow(wf)
    if parsed.errors:
        # 标记非法的工作流不做剔除，交由 inject_params 报错
        return wf, []
    out = copy.deepcopy(wf)
    pruned: list[str] = []
    for name, binding in parsed.params.items():
        if not binding.media:
            continue
        if name in values and values[name] is not None:
            continue
        if binding.required:
            # 必填媒体参数缺失：不剔除，交由 inject_params 报必填参数缺失
            continue
        node_id = binding.node_id
        pruned.append(name)
        # 剔除所有消费该节点的连线（同一 LoadImage 可能被正/负提示词节点共同引用）
        for nid, node in out.items():
            if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
                continue
            inputs = node["inputs"]
            for key in [
                k for k, v in inputs.items()
                if isinstance(v, list) and v and str(v[0]) == str(node_id)
            ]:
                del inputs[key]
        # 连线已全部剔除，删除 LoadImage 节点本身，避免占位图被执行
        out.pop(node_id, None)
    return out, pruned


def find_output_nodes(wf: dict) -> dict:
    """返回输出名 → 节点 ID 的映射，用于产物提取。"""
    mapping = {}
    for nid, node in wf.items():
        if not isinstance(node, dict):
            continue
        title = node.get("_meta", {}).get("title", "")
        for frag in [f.strip() for f in title.split(",")]:
            if frag.startswith("$output."):
                out_name = frag[8:]
                if out_name in mapping:
                    raise ValueError(f"输出 $output.{out_name} 被多个节点标记（节点 {mapping[out_name]} 与 {nid}），禁止静默覆盖")
                mapping[out_name] = nid
    return mapping
