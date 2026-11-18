"""工作流匹配器（P4/G1/G2）：按业务需求解析工作流，能力不足显式报错。

设计约束（v2 计划 §11.1/§11.2、§24 第 1/16 条）：
- **不按名称判断能力**：能力一律来自 `capabilities.normalize()`（从标记与节点读）。
- **选择优先级**：显式 workflow_id → 项目级默认 → 系统级默认(is_default) → 内置 → 能力匹配。
- **不允许静默降级**：显式指定但能力不足 → 抛 `workflow_capability_mismatch` 并列出缺失点。
- **项目默认不能绕过能力校验**：项目配置了不兼容工作流时同样报错，不偷偷换一个。

项目级默认存在 `project.settings["default_workflows"]`（不建新表，见审计附录 C.2 第 3 条）：
```json
{"default_workflows": {"image": "wf_x", "storyboard_image": "wf_y",
                       "video": "wf_z", "tts": "wf_t", "bgm": "wf_b", "sfx": "wf_s"}}
```
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from app.models.narrative import Project
from app.models.production import Workflow
from app.services import errors
from app.services.workflow import capabilities as caps_svc

# 业务槽位 → workflow_type（与 generate.py `_TARGET_WF_TYPE` 同源语义）
SLOT_WF_TYPE: dict[str, str] = {
    "image": "image",
    "storyboard_image": "image",
    "video": "video",
    "tts": "tts",
    "bgm": "bgm",
    "sfx": "sfx",
}

# 各业务槽位的默认必需能力。故意保守：只要求「能产出目标模态」，
# 具体槽位需求由调用方按 shot 情况追加（如 clip 需要 audio_input）。
# 注意：输出模态在这里仅作**声明与 UI 展示**用；`resolve()` 强制校验时会剔除
# `modalities.*_output`——输出节点多为第三方 class_type（VHS_VideoCombine、自定义
# Save 节点等），schema_builder 无法归类时会退化成 image，据此拒绝会误杀合法工作流。
# 输出模态由 `workflow_type == 期望类型` 这一条硬校验保证。
SLOT_REQUIRED: dict[str, dict] = {
    "image": {"modalities.image_output": True},
    "storyboard_image": {"modalities.image_output": True},
    "video": {"modalities.video_output": True},
    "tts": {"modalities.audio_output": True},
    "bgm": {"modalities.audio_output": True},
    "sfx": {"modalities.audio_output": True},
}

# 生成目标（generate.py 的 target）→ 项目默认槽位。
# target 是「任务目标」，槽位是「项目配置键」，两者不同名（keyframe vs storyboard_image）。
TARGET_SLOT: dict[str, str] = {
    "keyframe": "storyboard_image",
    "clip": "video",
    "audio": "tts",
    "bgm": "bgm",
    "sfx": "sfx",
}


@dataclass
class Resolution:
    """匹配结果：命中的工作流 + 决策来源 + 缺失能力（未命中时）。"""

    workflow: Workflow | None = None
    source: str = ""
    missing: list[str] = field(default_factory=list)
    considered: list[str] = field(default_factory=list)

    def require(self, *, slot: str) -> Workflow:
        if self.workflow is None:
            raise errors.biz_error(
                code="workflow_capability_mismatch",
                http=422,
                message=(
                    f"没有满足 {slot} 生成需求的工作流"
                    + (f"，缺失能力：{', '.join(self.missing)}" if self.missing else "")
                ),
                slot=slot,
                missing_capabilities=self.missing,
                considered_workflows=self.considered,
            )
        return self.workflow


def _caps_of(wf: Workflow) -> dict:
    """归一化能力；definition 缺失时退回原始 capabilities（不虚构能力）。"""
    if wf.definition:
        return caps_svc.normalize(
            wf.workflow_type, wf.definition,
            capabilities=wf.capabilities or {}, trigger=wf.trigger or {},
        )
    return dict(wf.capabilities or {})


def capability_enforceable(wf: Workflow) -> bool:
    """该工作流的能力声明是否可用于强制校验。

    只有「定义真实可解析」的工作流才做能力比对：`definition` 为空、只有画布残留、
    或解析出 0 参数 0 输出的历史/占位记录，其能力字段是未知而不是「不支持」。
    对未知能力做强制校验会把存量工作流全部判为不兼容（回归风险），因此这类记录
    只参与候选排序，不参与拒绝——但**显式指定**时仍会因类型不符被拦。
    """
    if not wf.definition:
        return False
    schema = caps_svc.build_schema(
        wf.definition, capabilities=wf.capabilities or {}, trigger=wf.trigger or {}
    )
    return bool(schema.get("params") or schema.get("outputs"))


def _check(wf: Workflow, required: dict) -> tuple[bool, list[str]]:
    """能力校验：不可强制校验的存量工作流按「满足」处理，避免误杀。"""
    if not capability_enforceable(wf):
        return True, []
    return caps_svc.capability_matches(_caps_of(wf), required)


def prefer_slot(name: str) -> str:
    """构造「优先选带某参考槽位的工作流」的偏好项（供调用方传入 resolve(prefer=...)）。"""
    return f"slot:{name}"


def _slot_params(capabilities: dict) -> set[str]:
    """从归一化能力或原始 capabilities 里取参考槽位参数名集合（两种形状都要兼容）。"""
    slots = (capabilities.get("reference") or {}).get("slots")
    if not slots:
        slots = capabilities.get("reference_slots") or []
    names: set[str] = set()
    for item in slots:
        if isinstance(item, str):
            names.add(item)
        elif isinstance(item, dict):
            param = item.get("param") or item.get("key")
            if param:
                names.add(str(param))
    return names


def _prefer_score(capabilities: dict, prefer: list[str]) -> int:
    """偏好命中计数：只用于候选排序，不参与是否可用的判定。"""
    score = 0
    for path in prefer:
        if path.startswith("slot:"):
            if path[5:] in _slot_params(capabilities):
                score += 1
            continue
        node: object = capabilities
        ok = True
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                ok = False
                break
            node = node[part]
        if ok and node not in (False, None, 0, "", []):
            score += 1
    return score


def project_defaults(db: Session, project_id: str | None) -> dict[str, str]:
    """读项目级默认工作流映射；项目不存在或未配置返回空 dict。"""
    if not project_id:
        return {}
    project = db.get(Project, project_id)
    if project is None:
        return {}
    raw = (project.settings or {}).get("default_workflows")
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(v, str) and v}


def save_project_defaults(db: Session, project_id: str, mapping: dict[str, str]) -> dict[str, str]:
    """保存项目级默认工作流；只接受 SLOT_WF_TYPE 内的槽位与真实存在的 workflow_id。"""
    project = db.get(Project, project_id)
    if project is None:
        raise errors.project_not_found()
    cleaned: dict[str, str] = {}
    for slot, wf_id in (mapping or {}).items():
        if slot not in SLOT_WF_TYPE:
            raise errors.biz_error(
                code="invalid_request", http=422,
                message=f"未知的工作流槽位：{slot}",
                allowed_slots=sorted(SLOT_WF_TYPE),
            )
        if not wf_id:
            continue
        wf = db.get(Workflow, wf_id)
        if wf is None:
            raise errors.biz_error(
                code="workflow_not_found", http=404,
                message=f"槽位 {slot} 指定的工作流不存在：{wf_id}",
            )
        expected = SLOT_WF_TYPE[slot]
        if wf.workflow_type != expected:
            raise errors.biz_error(
                code="invalid_request", http=422,
                message=f"槽位 {slot} 需要 {expected} 类型工作流，{wf_id} 是 {wf.workflow_type}",
            )
        cleaned[slot] = wf_id
    settings = dict(project.settings or {})
    settings["default_workflows"] = cleaned
    project.settings = settings
    db.commit()
    return cleaned


def resolve(
    db: Session,
    *,
    slot: str,
    project_id: str | None = None,
    workflow_id: str | None = None,
    extra_required: dict | None = None,
    prefer: list[str] | None = None,
) -> Resolution:
    """按优先级解析工作流，并做能力校验。

    - 显式 workflow_id：必须是真实存在、类型匹配、能力满足；否则**直接报错不回退**
    - 项目默认：命中后同样做能力校验；不足则报错（项目配置不能绕过校验）
    - 系统默认（is_default）/ 内置 / 任意：按顺序取第一个能力满足的

    `prefer` 是候选排序偏好（能力点路径，值需为 True 或 0），只在「没有项目默认」时
    影响候选顺序，不改变「能不能用」的判定：例如分镜图优先选带参考槽位的工作流
    （否则 @角色 无处注入），但若项目显式配置了默认工作流，则以项目配置为准。
    """
    expected_type = SLOT_WF_TYPE.get(slot)
    if expected_type is None:
        raise errors.biz_error(
            code="invalid_request", http=422,
            message=f"未知的工作流槽位：{slot}", allowed_slots=sorted(SLOT_WF_TYPE),
        )
    required = {
        k: v
        for k, v in {**SLOT_REQUIRED.get(slot, {}), **(extra_required or {})}.items()
        # 输出模态不参与强制校验，理由见 SLOT_REQUIRED 注释
        if not (k.startswith("modalities.") and k.endswith("_output"))
    }

    # 1) 显式指定：不满足就报错，绝不静默换工作流（§24 第 16 条）
    if workflow_id:
        wf = db.get(Workflow, workflow_id)
        if wf is None:
            raise errors.biz_error(
                code="workflow_not_found", http=404, message="工作流不存在",
                workflow_id=workflow_id,
            )
        if wf.workflow_type != expected_type:
            raise errors.biz_error(
                code="workflow_capability_mismatch", http=422,
                message=f"槽位 {slot} 需要 {expected_type} 类型工作流，当前是 {wf.workflow_type}",
                workflow_id=workflow_id, slot=slot,
            )
        ok, missing = _check(wf, required)
        if not ok:
            raise errors.biz_error(
                code="workflow_capability_mismatch", http=422,
                message=f"工作流 {wf.name} 不满足 {slot} 需求，缺失能力：{', '.join(missing)}",
                workflow_id=workflow_id, slot=slot,
                missing_capabilities=missing,
            )
        return Resolution(workflow=wf, source="explicit", considered=[wf.id])

    # 2) 项目级默认
    defaults = project_defaults(db, project_id)
    pid = defaults.get(slot)
    if pid:
        wf = db.get(Workflow, pid)
        if wf is None:
            raise errors.biz_error(
                code="workflow_not_found", http=404,
                message=f"项目默认工作流不存在（槽位 {slot}）：{pid}",
                slot=slot, workflow_id=pid,
            )
        if wf.workflow_type != expected_type:
            raise errors.biz_error(
                code="workflow_capability_mismatch", http=422,
                message=(
                    f"项目默认工作流 {wf.name} 类型为 {wf.workflow_type}，"
                    f"与槽位 {slot} 要求的 {expected_type} 不符"
                ),
                slot=slot, workflow_id=pid,
            )
        ok, missing = _check(wf, required)
        if not ok:
            raise errors.biz_error(
                code="workflow_capability_mismatch", http=422,
                message=(
                    f"项目默认工作流 {wf.name} 不满足 {slot} 需求，缺失能力：{', '.join(missing)}"
                    "（请在项目设置中更换默认工作流，或显式指定其他工作流）"
                ),
                slot=slot, workflow_id=pid, missing_capabilities=missing,
            )
        return Resolution(workflow=wf, source="project_default", considered=[wf.id])

    # 3) 系统默认 → 内置 → 任意（只看能力，不看名字）
    rows = (
        db.query(Workflow)
        .filter(Workflow.workflow_type == expected_type)
        .order_by(Workflow.is_default.desc(), Workflow.is_builtin.desc(), Workflow.created_at)
        .all()
    )
    if prefer:
        # 偏好优先于 is_default：分镜图/视频必须优先选「能注入参考图」的工作流，
        # 否则默认工作流即便被标记 is_default，也会因 0 槽位在注入期失败。
        # 同偏好得分内再按 is_default 优先，其余保持原相对顺序（稳定排序）。
        rows.sort(key=lambda w: (
            -_prefer_score(_caps_of(w), prefer),
            0 if w.is_default else 1,
        ))
    considered: list[str] = []
    for wf in rows:
        considered.append(wf.id)
        ok, _ = _check(wf, required)
        if ok:
            source = "system_default" if wf.is_default else "capability_match"
            return Resolution(workflow=wf, source=source, considered=considered)
    # 全部不满足：把每个候选的缺失点合并返回，便于定位
    merged: list[str] = []
    for wf in rows:
        _, missing = _check(wf, required)
        for item in missing:
            if item not in merged:
                merged.append(item)
    return Resolution(workflow=None, source="none", missing=merged, considered=considered)
