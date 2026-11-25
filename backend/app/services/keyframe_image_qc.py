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

# 最近一次 vision 评图失败原因（供告警消息携带，见 _call_vision_qc）
_LAST_VISION_ERROR = ""

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


def _image_to_data_uri(file_path: str, max_edge: int = 768, quality: int = 82) -> str | None:
    """将本地图片文件转为 data URI（先缩放 + 转 JPEG）。

    路径统一经 stored_path 归一化：历史资产 file_path 为 Windows 反斜杠形态
    （data\\media\\x.png），Linux 上 Path 不把反斜杠当分隔符，直接 exists() 恒为 False，
    会让参考图被全部丢弃、图像级质检静默降级成 reference_images_missing。

    必须缩放（2026-11-25 修复）：1024×1792 的 PNG 原图 base64 后单张 3.2MB，
    一镜「分镜图 + 3 张参考图」的评图请求体约 13MB。实测自定义网关直接
    HTTP 429 / 超时，`_call_vision_qc` 吞掉异常返回 None，于是所有分镜图的
    人物数量检查都退化成 vision_qc_unavailable 却仍标记 quality_status=passed
    （镜 9 期望 2 人只画出 1 人，一路静默通过）。缩到长边 768、JPEG q82 后
    单张约 60-90KB，请求体从 13MB 降到 ~300KB。
    """
    from app.core.config import stored_path

    path = stored_path(file_path)
    if not path.exists():
        return None
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(path) as im:
            im = im.convert("RGB")
            w, h = im.size
            if max(w, h) > max_edge:
                scale = max_edge / float(max(w, h))
                im = im.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
            buf = BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True)
        b64 = base64.b64encode(buf.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{b64}"
    except Exception:  # noqa: BLE001  PIL 不可用或图片损坏时回退原图
        try:
            with open(path, "rb") as f:
                raw = f.read()
        except OSError:
            return None
        b64 = base64.b64encode(raw).decode("ascii")
        mime = {
            ".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".webp": "image/webp",
        }.get(path.suffix.lower(), "image/png")
        return f"data:{mime};base64,{b64}"


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


def _person_count_rule(db: Session, shot: Shot) -> str:
    """人物数量硬性检查项。

    原先四项检查（身份/道具/构图/光线）不含人数断言，镜 9 期望「林小满、陈默」
    两人却只画出林小满，vision 只报了光线和道具问题，人数缺失一路通过。

    角色名单必须与「生成时实际绑定的人数」同源，否则质检和提示词各说一套：
    - 首选 shot_cast.resolve_shot_cast —— 提示词编译器 _shot_characters 用的就是它，
      有契约时以 shot_plan 为准（画外音不占人数）
    - 兜底 shot_refs.shot_subject_characters —— 无契约时的旧启发式
    实测镜 9：提示词写「必须等于 2 人（林小满、陈默）」，而只读 shot_subject_characters
    得到 1 人，质检因此认为少人也没问题。不要用 shot.cast 关系：实测该关系为空。
    """
    names: list[str] = []
    try:
        from app.services.shot_cast import resolve_shot_cast

        names = [
            cc.character.name
            for cc in resolve_shot_cast(db, shot).characters
            if getattr(cc.character, "name", None)
        ]
    except Exception:  # noqa: BLE001
        names = []
    if not names:
        try:
            from app.services.shot_refs import shot_subject_characters

            names = [c.name for c in shot_subject_characters(db, shot) if getattr(c, "name", None)]
        except Exception:  # noqa: BLE001  关系未加载时退化为通用规则
            names = []
    if not names:
        return (
            "5. person_count: 数出画面中的真人数量；同一角色不得重复出现"
            "（分身/镜像/重影），出现重复必须报 issue（code=person_count_mismatch）。"
        )
    uniq = list(dict.fromkeys(names))
    listed = "、".join(uniq)
    return (
        f"5. person_count: 本镜必须出场的角色共 {len(uniq)} 位（{listed}）。"
        "请逐一确认每位角色都真实出现在画面中（要有完整人物，不能只有局部/背影/倒影），"
        "并数出画面中的真人总数。缺少任一角色、多出未声明人物、或同一角色重复出现"
        "（分身/镜像/重影），都必须报 issue（code=person_count_mismatch），"
        f"message 里写明「期望 {len(uniq)} 人，实际 M 人，缺 X」。"
    )


def _build_image_qc_prompt(db: Session, shot: Shot, plan_posture_block: str) -> str:
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
        _person_count_rule(db, shot),
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

    # mock 渲染产出的只是占位图，送去评图既无意义，又会白跑一次真实 LLM 请求
    # （实测：render_mock 的用例会卡在真实 vision 请求上直到超时 99s）。
    if getattr(settings, "render_mock", False):
        logger.info("vision QC skipped: render_mock enabled")
        return None

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
        # 评图必须用支持 image_url 的模型：settings.llm_model 是文本模型
        # （deepseek-v4.x），实测网关对带图请求返回 400/502「Trae 上游未接受本次请求」，
        # 异常被吞后所有分镜图质检静默降级成 passed。vision_model 可配置，
        # 留空回落到实测可读图的 glm-5.3-flash。
        _vision_model = (getattr(settings, "vision_model", "") or "").strip() or "glm-5.3-flash"
        client = LLMClient(base_url=base_url, api_key=api_key, model=_vision_model)
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
        # 不再只写 logger.warning：uvicorn 未配置 logging 时这条日志根本不会进
        # journal，线上只能看到一句「vision_qc_unavailable」，无法定位是网关
        # 429、超时还是模型不支持图片。把原因记到模块级变量，由 _run_image_qc
        # 带进告警消息，前端事件流里直接可见。
        global _LAST_VISION_ERROR
        _LAST_VISION_ERROR = f"{type(exc).__name__}: {str(exc)[:300]}"
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

    system_prompt = _build_image_qc_prompt(db, shot, plan_posture_block)
    result = _call_vision_qc(system_prompt, keyframe_uri, ref_images)

    if result is None:
        issues.append(KeyframeQCIssue(
            code="vision_qc_unavailable",
            message=(
                "多模态 vision 质检不可用（provider 不支持或调用失败），跳过图像级检查"
                + (f"：{_LAST_VISION_ERROR}" if _LAST_VISION_ERROR else "")
            ),
            entity_id=shot.id,
            entity_type="shot",
        ))
        return True, issues

    if isinstance(result, dict):
        for item in result.get("issues") or []:
            if isinstance(item, dict):
                # 字段兼容：提示词约定 code/message，但真实供应商（实测 deepseek 网关）
                # 会返回 type/description。只认 code/message 会把告警解析成空消息，
                # 前端看到一条无名 issue 等于没告警，因此两种写法都要接。
                code = item.get("code") or item.get("type") or "vision_qc_issue"
                message = item.get("message") or item.get("description") or ""
                issues.append(KeyframeQCIssue(
                    code=code,
                    message=message,
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
