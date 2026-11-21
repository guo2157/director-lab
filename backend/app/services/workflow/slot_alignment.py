"""工作流参考图槽位三方一致性守卫（2026-11-21）。

提示词里的 `<Picture N>`、页面上的「参考图 N」、以及模型真正收到的第 N 张图，
三者能对上的前提是**下面三份数据严格一致**：

| 来源 | 位置 | 作用 |
|---|---|---|
| ① 工作流 JSON 节点键名 | `definition[<node>].inputs["ref_images.ref_image_N"]` | 决定模型收到的图片顺序（N 越小越靠前）|
| ② capabilities 声明 | `capabilities.reference.slots[].input_key` | 把业务槽位名（ref_identity…）映射到 ① 的键名 |
| ③ 提示词编号 | `<Picture N>`（1-based，N = 键序号 + 1）| 告诉模型「第 N 张图是干什么的」|

任何一份漂移，症状都长得一样：**编号与图片错位、角色图被当成帧图**。
实测过的真实事故就是 ② 与 ③ 分叉：上一镜没有尾帧 → `ref_image_0` 空 →
执行器压缩后分镜图变成第 1 张，而提示词仍写「Picture 1 = 上一镜头尾帧」。

`verify_reference_alignment()` 把这层约定变成可断言的检查：上传/导入工作流后跑一次，
或在测试里对真实 workflow 记录跑一次，漂移立刻暴露，不用等出图才发现。
"""
from __future__ import annotations

import re

_REFIMG_RE = re.compile(r"^ref_images\.ref_image_(\d+)$")
_IMAGE_RE = re.compile(r"^images\.image_(\d+)$")
_MEDIA_PREFIXES = ("ref_images.", "images.", "ref_audios.", "audios.", "videos.")


def _definition_media_keys(definition: dict | None) -> dict[str, list[str]]:
    """{input_key: [node_id, ...]}：definition 里真实存在的媒体输入键。"""
    out: dict[str, list[str]] = {}
    if not isinstance(definition, dict):
        return out
    for nid, node in definition.items():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for key in inputs:
            if isinstance(key, str) and key.startswith(_MEDIA_PREFIXES):
                out.setdefault(key, []).append(str(nid))
    return out


def _marker_node(definition: dict | None, param: str) -> str | None:
    """在 definition 里找标题标记 `$<param>.~field` 所在节点，返回 node_id。

    旧格式 capabilities 只写槽位名，注入目标靠节点标题上的 `$param.field` 标记。
    标记可能出现在标题中段（如「H3 Ref2VA, $prompt.prompt!, $width!」），
    所以用正则匹配 `$param` 后紧跟 `.` 或 `,`，不能只判断前缀。
    """
    if not isinstance(definition, dict) or not param:
        return None
    pattern = re.compile(rf"\${re.escape(param)}\s*[.,]")
    for nid, node in definition.items():
        if not isinstance(node, dict):
            continue
        title = str((node.get("_meta") or {}).get("title") or "")
        if pattern.search(title):
            return str(nid)
    return None


def verify_reference_alignment(definition: dict | None, capabilities: dict | None) -> list[dict]:
    """校验 capabilities 的槽位声明与工作流 JSON 节点键名是否严格对齐。

    返回 issue 列表，空列表表示对齐。分两级：

    - ``error``：声明了 JSON 里不存在的 input_key / 同一键被两个槽位占用 /
      同一槽位重复声明。这类错误会让**注入落到不存在的节点**，或让两个槽位
      抢同一张图，编号必然错位。
    - ``warning``：声明顺序与键名序号不一致（`compressed_numbers` 内部会按
      键序号排序，所以不会算错编号，但页面「参考图 N」的展示顺序会与模型
      接收顺序不同，容易误读）；图片语法混用（同时声明 `ref_images.*` 与
      `images.*`，`workflow_ref_syntax()` 只能取其一，另一种会被写错标记）。
    """
    from app.services.workflow.capabilities import declared_reference_slots

    issues: list[dict] = []
    keys = _definition_media_keys(definition)
    slots = declared_reference_slots(capabilities)
    if not slots:
        return issues

    seen_param: dict[str, int] = {}
    seen_key: dict[str, str] = {}
    image_keys: list[tuple[str, str]] = []
    syntaxes: set[str] = set()

    for item in slots:
        if not isinstance(item, dict):
            issues.append({
                "level": "error",
                "code": "SLOT_DECLARATION_INVALID",
                "message": f"槽位声明不是对象：{item!r}",
            })
            continue
        param = str(item.get("param") or item.get("key") or "")
        input_key = str(item.get("input_key") or "")
        if not input_key:
            # 向后兼容：旧格式把槽位写成裸字符串（["ref_identity", ...]），
            # input_key 由工作流节点标题标记 `$ref_identity.~image` 推断。
            # 只要 definition 里存在该参数标记的节点，注入就能落到真实节点上，
            # 不算漂移；只有连节点都没有才是真错误。
            marker_node = _marker_node(definition, param)
            if marker_node is not None:
                issues.append({
                    "level": "warning",
                    "code": "SLOT_INPUT_KEY_INFERRED",
                    "param": param,
                    "message": (
                        f"槽位 {param} 未声明 input_key，将按节点 {marker_node} "
                        "的标题标记推断（旧格式声明，建议补全 input_key）"
                    ),
                })
                continue
            issues.append({
                "level": "error",
                "code": "SLOT_INPUT_KEY_MISSING",
                "param": param,
                "message": (
                    f"槽位 {param} 未声明 input_key，且工作流里没有对应的节点标记 "
                    f"（$${param}.<field>），无法定位注入目标"
                ),
            })
            continue

        seen_param[param] = seen_param.get(param, 0) + 1
        if seen_param[param] > 1:
            issues.append({
                "level": "error",
                "code": "SLOT_PARAM_DUPLICATED",
                "param": param,
                "message": f"槽位 {param} 被声明了 {seen_param[param]} 次，注入时会互相覆盖",
            })

        # 具名槽位（first_frame/end_frame 等）的 input_key 是节点上的字段名（image），
        # 注入靠节点标题标记 `$first_frame.image` 定位。多个具名槽位共用同名字段
        # （各自落在不同节点）是正常配置，不能按「抢同一张图」判错。
        # 图序相关的检查只对有媒体前缀的键（ref_images.* / images.*）成立。
        if not input_key.startswith(_MEDIA_PREFIXES):
            continue

        owner = seen_key.get(input_key)
        if owner is not None and owner != param:
            issues.append({
                "level": "error",
                "code": "SLOT_INPUT_KEY_SHARED",
                "param": param,
                "input_key": input_key,
                "message": (
                    f"input_key {input_key} 同时被槽位 {owner} 与 {param} 占用，"
                    "两个槽位会抢同一张图，图序必然错位"
                ),
            })
        seen_key[input_key] = param

        nodes = keys.get(input_key)
        if not nodes:
            issues.append({
                "level": "error",
                "code": "SLOT_INPUT_KEY_NOT_IN_WORKFLOW",
                "param": param,
                "input_key": input_key,
                "message": (
                    f"槽位 {param} 声明的 {input_key} 在工作流 JSON 里不存在，"
                    "注入会静默丢失（模型收不到这张图）"
                ),
            })
            continue
        if len(nodes) > 1:
            issues.append({
                "level": "warning",
                "code": "SLOT_INPUT_KEY_AMBIGUOUS",
                "param": param,
                "input_key": input_key,
                "message": (
                    f"{input_key} 同时出现在节点 {nodes}，注入只改其一，"
                    "另一处仍是占位图"
                ),
            })

        if _REFIMG_RE.match(input_key):
            syntaxes.add("picture")
            image_keys.append((str(param), input_key))
        elif _IMAGE_RE.match(input_key):
            syntaxes.add("image")
            image_keys.append((str(param), input_key))

    if len(syntaxes) > 1:
        issues.append({
            "level": "warning",
            "code": "SLOT_SYNTAX_MIXED",
            "message": (
                "同一工作流同时声明了 ref_images.*（<Picture N>）与 images.*（<imageN>）"
                "两种图片语法，提示词只能选一种标记，另一种会被写错"
            ),
        })

    # 声明顺序 vs 键名序号：压缩编号按键序号排序，这里只提示展示顺序差异
    ordered = [
        (int(_REFIMG_RE.match(k).group(1)) + 1 if _REFIMG_RE.match(k)
         else int(_IMAGE_RE.match(k).group(1)), p)
        for p, k in image_keys
    ]
    if ordered != sorted(ordered):
        issues.append({
            "level": "warning",
            "code": "SLOT_DECLARATION_ORDER",
            "message": (
                "槽位声明顺序与工作流键名序号顺序不一致：提示词编号按序号计算不会错，"
                "但页面「参考图 N」的排列顺序会与模型接收顺序不同"
            ),
        })
    return issues
