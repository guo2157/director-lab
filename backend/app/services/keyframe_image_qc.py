"""分镜图出图质检（Phase 4）：只告警不阻断。

分两层：
1. 提示词级 — 委托给 ``app.services.prompt_qc.apply_keyframe_qc``（确定性校验 + LLM 复核）
2. 图像级 — 调用多模态模型评图（vision provider 可用时），检查：
   - 角色身份一致性（脸部/发型/服装 vs 定妆图）
   - 道具可见性与完整性（锚定图 vs 出图）
   - 构图偏差（站位/视线/景别 vs 契约 plan_posture_block）
   - 光线方向（与场景基准图主光方向一致性）

规则（方案 §4）：
- **质检只告警不阻断**：所有问题的 severity 均为 warn，调用方自行决定是否重生成
- vision 不可用时跳过图像级检查，记一条 capability_unavailable 告警
- 复用 ``prompt_qc.apply_keyframe_qc`` 做提示词级检查（它的硬阻断性质在本模块被降级为告警）
- 不恢复人工审核闸口
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Asset, Character, Prop, PropShotState, Scene, Shot

logger = logging.getLogger(__name__)

# ── 质检结果数据结构 ──────────────────────────────────────────


@dataclass
class KeyframeQCIssue:
    """单个质检告警。"""

    code: str
    message: str
    severity: str = "warn"  # warn 唯一值，不阻断
    entity_id: str | None = None
    entity_type: str | None = None
    detail: dict = field(default_factory=dict)


@dataclass
class KeyframeQCResult:
    """分镜图出图质检结论：pass_ 仅表示无阻断级问题，不影响 warn 是否存在。"""

    pass_: bool = True  # 始终 True（本模块不阻断）
    issues: list[KeyframeQCIssue] = field(default_factory=list)
    prompt_qc_ok: bool = True
    image_qc_ok: bool = True  # vision 不可用时 True（跳过不算失败）

    @property
    def warning_count(self) -> int:
        return len([i for i in self.issues if i.severity == "warn"])

    def to_dict(self) -> dict:
        return {
            "pass": self.pass_,
            "issues": [
                {"code": i.code, "message": i.message, "severity": i.severity,
                 "entity_id": i.entity_id, "entity_type": i.entity_type,
                 "detail": i.detail}
                for i in self.issues
            ],
            "prompt_qc_ok": self.prompt_qc_ok,
            "image_qc_ok": self.image_qc_ok,
            "warning_count": self.warning_count,
        }


# ── 提示词级质检（委托 prompt_qc）──────────────────────────────

def _run_prompt_qc(db: Session, shot: Shot, prompt: str) -> tuple[bool, list[KeyframeQCIssue]]:
    """调用 apply_keyframe_qc，将其硬阻断降级为告警。"""
    issues: list[KeyframeQCIssue] = []
    try:
        from app.services.prompt_qc import apply_keyframe_qc
        _ = apply_keyframe_qc(db, shot, prompt, with_llm=False)
        return True, issues
    except Exception as exc:
        code = getattr(exc, "code", None) or "prompt_qc_blocked"
        issues.append(KeyframeQCIssue(
            code=code,
            message=f"提示词级质检触发硬阻断（已降级为告警）：{exc}",
            entity_id=shot.id,
            entity_type="shot",
        ))
        return False, issues


# ── 图像级质检（vision）────────────────────────────────────────


def _image_to_data_uri(file_path: str) -> str | None:
    """将本地图片文件转为 data URI。"""
    path = Path(file_path)
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            raw = f.read()
        b64 = base64.b64encode(raw).decode("ascii")
        suffix = path.suffix.lower()
        mime = {
            ".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".webp": "image/webp",
        }.get(suffix, "image/png")
        return f"data:{mime};base64,{b64}"
    except OSError:
        return None


def _gather_reference_images(db: Session, shot: Shot) -> list[dict]:
    """收集本镜关键帧生成所用的参考图（角色定妆/场景基准/道具锚定）。"""
    images: list[dict] = []
    scene = db.get(Scene, shot.scene_id)
    if scene is None:
        return images
    project_id = scene.project_id

    # 场景基准图
    if scene.baseline_asset_id:
        asset = db.get(Asset, scene.baseline_asset_id)
        if asset is not None and asset.file_path:
            data_uri = _image_to_data_uri(asset.file_path)
            if data_uri:
                images.append({"type": "scene", "name": scene.title or "场景",
                               "data_uri": data_uri})

    # 角色定妆图
    from app.services.shot_cast import resolve_shot_cast
    cast = resolve_shot_cast(db, shot)
    for cc in cast.characters:
        ch = cc.character
        if ch.portrait_asset_id:
            asset = db.get(Asset, ch.portrait_asset_id)
            if asset is not None and asset.file_path:
                data_uri = _image_to_data_uri(asset.file_path)
                if data_uri:
                    images.append({"type": "character", "name": ch.name,
                                   "data_uri": data_uri, "character_id": ch.id})

    # 道具锚定图
    for cp in cast.props:
        prop = cp.prop
        if cp.reference_required and prop.anchor_asset_id:
            asset = db.get(Asset, prop.anchor_asset_id)
            if asset is not None and asset.file_path:
                data_uri = _image_to_data_uri(asset.file_path)
                if data_uri:
                    images.append({"type": "prop", "name": prop.name,
                                   "data_uri": data_uri, "prop_id": prop.id})

    return images


def _build_image_qc_prompt(shot: Shot, plan_posture_block: str) -> str:
    """构造 vision 质检的系统提示词。"""
    plan = shot.shot_plan if isinstance(shot.shot_plan, dict) else None
    plan_comp = (plan or {}).get("composition") or {}
    if not isinstance(plan_comp, dict):
        plan_comp = {}

    parts = [
        "你是一个分镜图质检专家。请检查这张分镜图（第一张用户图片），",
        "对照后续的参考图，判断是否符合以下契约要求。仅输出一个 JSON 对象。",
        "",
        "检查项：",
        "1. character_identity: 每个角色的脸部、发型、服装是否与定妆图一致（无身份漂移、多人同框、分身）。",
        "2. prop_visibility: 声明的可见道具是否出现在图中，形状/颜色与锚定图是否一致，无额外道具。",
        "3. composition: 画面构图是否与契约一致。站位线索：" + (plan_posture_block or "未指定") + "；"
        "构图要求：" + " ".join(v for v in [
            plan_comp.get("framing", ""), plan_comp.get("focus", ""),
            plan_comp.get("relation_note", "")
        ] if v).strip(),
        "4. lighting: 光线主方向是否从左上照入、投影落向右下（与场景基准图一致），无多光源或方向矛盾。",
        "",
        "输出格式（严格 JSON，不包含任何其他文本）：",
        '{"pass": true/false, "issues": [{"code": "...", "severity": "warn", "message": "...", "detail": {...}}]}',
        "",
        "规则：所有 issue 的 severity 必须为 warn；不存在问题则 issues 为空数组、pass 为 true。",
    ]
    return "\n".join(parts)


def _call_vision_qc(
    system_prompt: str,
    keyframe_data_uri: str,
    ref_images: list[dict],
) -> dict | None:
    """调用多模态 LLM 评图，返回解析后的 JSON dict 或 None。"""
    settings = get_settings()

    # 检查 vision 可用性：provider 已配置且模型支持多模态
    provider = (settings.llm_provider or "").strip()
    vision_providers = {"openai", "custom"}
    if not provider or provider not in vision_providers:
        logger.info("vision QC skipped: provider=%s not in vision_providers", provider)
        return None

    base_url = (settings.llm_base_url or "").strip()
    api_key = (settings.llm_api_key or "").strip()
    if not base_url:
        logger.info("vision QC skipped: no llm_base_url configured")
        return None

    # 构建多模态 messages
    user_content: list[dict] = []
    user_content.append({
        "type": "image_url",
        "image_url": {"url": keyframe_data_uri},
    })
    for ref in ref_images:
        label = f"{ref['type']}_reference: {ref['name']}"
        user_content.append({"type": "text", "text": label})
        user_content.append({
            "type": "image_url",
            "image_url": {"url": ref["data_uri"]},
        })

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    try:
        from app.integrations.llm import LLMClient
        client = LLMClient(base_url=base_url, api_key=api_key, model=settings.llm_model)
        # chat_messages 仅支持 json_mode / max_tokens（temperature 固定 0.3，
        # 超时由 LLMClient 构造函数 timeout 控制）。此前传 temperature/timeout_seconds
        # 会抛 TypeError 并被下方 except 吞掉，导致图像级质检从未真正执行。
        result = client.chat_messages(messages, json_mode=True, max_tokens=4096)
        text = ""
        if isinstance(result, dict):
            text = result.get("content") or result.get("message") or ""
        elif isinstance(result, str):
            text = result
        if not text:
            return None
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("\n```", 1)[0] if "\n```" in text else text[3:]
        text = text.strip()
        return json.loads(text) if text else None
    except Exception as exc:
        logger.warning("vision QC call failed: %s", exc)
        return None


def _run_image_qc(
    db: Session,
    shot: Shot,
    keyframe_path: str,
    plan_posture_block: str,
) -> tuple[bool, list[KeyframeQCIssue]]:
    """执行图像级质检，vision 不可用时返回 ok=True + 一条 capability_unavailable 告警。"""
    issues: list[KeyframeQCIssue] = []

    keyframe_uri = _image_to_data_uri(keyframe_path)
    if keyframe_uri is None:
        issues.append(KeyframeQCIssue(
            code="keyframe_image_unreadable",
            message=f"无法读取分镜图文件：{keyframe_path}",
            entity_id=shot.id,
            entity_type="shot",
        ))
        return False, issues

    ref_images = _gather_reference_images(db, shot)
    if not ref_images:
        issues.append(KeyframeQCIssue(
            code="reference_images_missing",
            message="本镜无可用的参考图（场景基准/角色定妆/道具锚定），跳过图像级质检",
            entity_id=shot.id,
            entity_type="shot",
        ))
        return True, issues

    system_prompt = _build_image_qc_prompt(shot, plan_posture_block)
    result = _call_vision_qc(system_prompt, keyframe_uri, ref_images)

    if result is None:
        issues.append(KeyframeQCIssue(
            code="vision_qc_unavailable",
            message="多模态 vision 质检不可用（provider 不支持或调用失败），跳过图像级检查",
            entity_id=shot.id,
            entity_type="shot",
        ))
        return True, issues

    if isinstance(result, dict):
        for item in result.get("issues") or []:
            if isinstance(item, dict):
                issues.append(KeyframeQCIssue(
                    code=item.get("code", "vision_qc_issue"),
                    message=item.get("message", ""),
                    severity="warn",
                    entity_id=shot.id,
                    entity_type="keyframe",
                    detail=item.get("detail") or {},
                ))
        if not result.get("pass", True):
            return False, issues
    return True, issues


# ── 统一入口 ──────────────────────────────────────────────────


def apply_keyframe_image_qc(
    db: Session,
    shot: Shot,
    prompt: str,
    keyframe_path: str,
    *,
    plan_posture_block: str = "",
) -> KeyframeQCResult:
    """分镜图出图质检统一入口（只告警不阻断）。

    Args:
        db: 数据库会话
        shot: 目标镜头
        prompt: 生成该分镜图使用的提示词
        keyframe_path: 出图文件的本地路径（图像级质检需要）
        plan_posture_block: 契约编译输出的站位/姿态/视线提示块（Phase 3）

    Returns:
        KeyframeQCResult：pass_ 始终 True，issues 收集全部告警
    """
    result = KeyframeQCResult()

    # Layer 1: 提示词级质检（复用 prompt_qc，硬阻断降级为告警）
    prompt_ok, prompt_issues = _run_prompt_qc(db, shot, prompt)
    result.prompt_qc_ok = prompt_ok
    result.issues.extend(prompt_issues)

    # Layer 2: 图像级质检（vision，不可用时跳过）
    image_ok, image_issues = _run_image_qc(db, shot, keyframe_path, plan_posture_block)
    result.image_qc_ok = image_ok
    result.issues.extend(image_issues)

    return result
