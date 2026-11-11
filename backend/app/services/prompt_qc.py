"""分镜图提示词质量检查（三层：编号绑定 / 确定性门禁 / LLM 语义复核）。

背景（2026-11 实测 + 公开对照实验）：
- 多参考图生图时，裸 `@名称` 无法把「名字」与「第几张图」绑定；参考图达到 3 张以上，
  角色左右站位互换与身份混同的概率显著上升。把引用改写成「图N（角色定妆图）：@名称」，
  并在同一句里绑定站位，可把身份+位置正确率从 66% 提升到 98%（Blooper 50 场景对照实验）。
- 道具若没有锚定图会被静默跳过，提示词里只剩文字描述，道具身份完全不受控。

本模块对应三层保障：
1. relabel_reference_block：按实际注入槽位重写『参考图职责』，绑定图序并补站位约束（确定性、幂等）；
2. check_keyframe_prompt：角色站位缺失 / 道具锚定图缺失的确定性校验（硬门禁）；
3. llm_audit_keyframe_prompt：LLM 语义复核，返回问题清单与可选的修正后提示词（失败不阻断）。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.orm import Session

from app.services import errors
from app.models import Character, Prop, PropShotState, Scene, Shot

logger = logging.getLogger(__name__)

_ROLE_LABEL = {"character": "角色定妆图", "scene": "场景基准图", "prop": "道具锚定图"}
_ROLE_KEEP = {
    "character": "保持{t}中的角色外形、服饰与配色不变",
    "scene": "保持{t}中的场景结构、材质与光照方向",
    "prop": "道具外观须与{t}完全一致",
}

# 站位/空间词：命中任一即视为该角色已给出画面位置
_POSITION_TOKENS = (
    "画面左", "画面右", "左侧", "右侧", "左边", "右边", "偏左", "偏右", "居左", "居右",
    "中部", "中央", "正中", "前景", "背景", "上方", "下方", "角落", "边缘",
    "画左", "画右", "左上", "右上", "左下", "右下",
)

_SECTION_RE = re.compile(
    r"^\s*(任务|参考图职责|美术风格|目标画面|必须保持|允许变化|禁止|输出)\s*[:：]"
)


def _section_bounds(lines: list[str], name: str) -> tuple[int, int] | None:
    """返回某个分段（含标题行）的 [start, end) 行号区间。"""
    start = None
    for i, line in enumerate(lines):
        m = _SECTION_RE.match(line)
        if not m:
            continue
        if start is None:
            if m.group(1) == name:
                start = i
            continue
        return start, i
    if start is not None:
        return start, len(lines)
    return None


def relabel_reference_block(
    db: Session,
    project_id: str,
    prompt: str,
    media: dict[str, str] | None,
    reference_slots: list | None,
) -> str:
    """按实际注入槽位的图序重写『参考图职责』分段。

    - 图序与媒体槽位严格对应（slot_image_index），避免「文字说的图N」与「模型收到的第N张」错位；
    - 每个引用附带身份/外观锁定句；多角色追加站位固定约束（不得新增、不得左右互换）；
    - 提示词中没有『参考图职责』分段时不新建，保持原样（由上游模板负责）。
    """
    if not prompt or not media or not reference_slots:
        # 无槽位声明时无法确定图序，宁可不改也不写错编号
        return prompt
    from app.services.ref_inject import resolve_refs, slot_image_index, workflow_ref_syntax

    syntax = workflow_ref_syntax(reference_slots)
    if syntax is None:
        return prompt

    def _tag(idx: int) -> str:
        return f"<image{idx}>" if syntax == "image" else f"<Picture {idx}>"

    try:
        refs = resolve_refs(db, project_id, prompt)
    except Exception:  # noqa: BLE001 - 引用解析失败保持原样，由既有门禁负责报错
        return prompt
    path_to_slot = {path: slot for slot, path in media.items()}
    entries: list[tuple[int, str, str]] = []  # (图序, 角色标签, 名称)
    char_count = 0
    for name, info in refs.items():
        slot = path_to_slot.get(info.get("file_path"))
        if slot is None:
            continue
        idx = slot_image_index(slot, reference_slots)
        if idx is None:
            continue
        kind = info.get("type") or ""
        if kind == "character":
            char_count += 1
        entries.append((idx, kind, name))
    if not entries:
        return prompt
    entries.sort(key=lambda item: item[0])

    lines = prompt.split("\n")
    bounds = _section_bounds(lines, "参考图职责")
    if bounds is None:
        return prompt
    start, end = bounds
    block = ["参考图职责（图序与生成时实际注入的参考图严格一致）："]
    for idx, kind, name in entries:
        tag = _tag(idx)
        keep = _ROLE_KEEP.get(kind, "").format(t=tag)
        suffix = f" —— {keep}" if keep else ""
        block.append(f"- {tag}（{_ROLE_LABEL.get(kind, '参考图')}）：@{name}{suffix}")
    if char_count >= 2:
        block.append(
            "角色站位固定：只出现上述引用的角色，不得新增、替换或合并角色，"
            "不得左右互换站位；每个角色的画面位置以其在图中的方位描述为准。"
        )
    return "\n".join(lines[:start] + block + lines[end:])


def _visible_props(db: Session, shot: Shot, prompt: str) -> list[Prop]:
    """本镜需要参考图的道具：正文提及（裸名/@引用）+ 生命周期状态可见的并集。"""
    scene = db.get(Scene, shot.scene_id)
    if scene is None:
        return []
    states = {
        st.prop_id: st
        for st in db.query(PropShotState)
        .filter(PropShotState.project_id == scene.project_id, PropShotState.shot_id == shot.id)
        .all()
    }
    result: list[Prop] = []
    for prop in db.query(Prop).filter(Prop.project_id == scene.project_id).all():
        if not prop.name:
            continue
        st = states.get(prop.id)
        if st is not None and st.visibility == "unknown" \
                and st.existence_state == "not_introduced" \
                and st.interaction_state == "unassigned" \
                and not st.reference_required:
            st = None
        # 正文提及（含裸名或 @引用）即视为本镜需要该道具
        wanted = f"@{prop.name}" in prompt or prop.name in prompt
        if st is not None:
            if st.existence_state in ("not_introduced", "destroyed", "consumed"):
                wanted = False
            elif st.visibility == "visible" and st.reference_required:
                wanted = True
        if wanted:
            result.append(prop)
    return result


def _sentence_around(text: str, name: str) -> str:
    """取包含 @名称 的句子（按中英文句读切分），用于判断站位是否在同一句内。"""
    token = f"@{name}"
    for seg in re.split(r"[；;。\n]", text):
        if token in seg:
            return seg
    return ""


def check_keyframe_prompt(db: Session, shot: Shot, prompt: str) -> list[dict[str, str]]:
    """确定性校验：道具锚定图缺失（硬错误）+ 多角色站位缺失（可修复问题）。"""
    issues: list[dict[str, str]] = []
    for prop in _visible_props(db, shot, prompt):
        if not prop.anchor_asset_id:
            issues.append(
                {
                    "code": "prop_anchor_missing",
                    "level": "error",
                    "detail": f"道具『{prop.name}』在本镜可见但缺少锚定图，无法保证外观一致",
                }
            )
    from app.services.ref_inject import extract_refs

    scene = db.get(Scene, shot.scene_id)
    if scene is not None:
        chars = [
            ch.name
            for ch in db.query(Character).filter(Character.project_id == scene.project_id).all()
        ]
        used = [n for n in extract_refs(prompt) if n in set(chars)]
        if len(used) >= 2:
            target = ""
            bounds_text = prompt
            m = re.search(r"目标画面[:：]", prompt)
            if m:
                target = prompt[m.end():]
            for name in used:
                seg = _sentence_around(target or bounds_text, name)
                if not seg or not any(tok in seg for tok in _POSITION_TOKENS):
                    issues.append(
                        {
                            "code": "character_position_missing",
                            "level": "warn",
                            "detail": f"角色『{name}』未在同一句内给出画面站位（左/右/中/前景等）",
                        }
                    )
    return issues


_SYSTEM = """你是分镜图提示词质检员。给定镜头信息与最终提示词，检查两项：
1. 多角色站位：每个出场角色的画面位置是否明确且互不冲突（左右/前后/中景），是否可能被模型互换；
2. 道具完整性：本镜可见道具是否都在参考图职责中出现并绑定图序，是否存在只写文字没有参考图的道具。
只输出 JSON：{"ok": true/false, "issues": [{"code": "...", "detail": "..."}], "fixed_prompt": "修正后的完整提示词，无问题或无法修正时为空字符串"}。
允许整段重写以彻底修正站位与道具引用，但必须保留：剧情内容、美术风格、必须保持/允许变化/禁止分段、参考图的图序与标签格式；不得新增或删除角色、道具。"""


def llm_audit_keyframe_prompt(
    db: Session, shot: Shot, prompt: str, issues: list[dict[str, str]]
) -> str | None:
    """LLM 语义复核，返回修正后的提示词；未启用/调用失败时返回 None（不阻断生成）。"""
    try:
        from app.integrations.llm import LLMClient

        scene = db.get(Scene, shot.scene_id)
        manifest = {
            "characters": [i["detail"] for i in issues if i["code"] == "character_position_missing"],
            "scene": scene.title if scene is not None else None,
            "props": [
                {"name": p.name, "has_anchor": bool(p.anchor_asset_id)}
                for p in _visible_props(db, shot, prompt)
            ],
        }
        user = (
            f"镜头：第{shot.episode}集第{shot.seq}镜，景别 {shot.shot_type or '未标注'}\n"
            f"已知问题：{issues}\n资产清单：{manifest}\n\n提示词：\n{prompt}"
        )
        data: dict[str, Any] = LLMClient().chat_json(_SYSTEM, user)
    except Exception as exc:  # noqa: BLE001 - LLM 异常不阻断出图
        logger.warning("prompt_qc llm audit skipped: %s", exc)
        return None
    fixed = str(data.get("fixed_prompt") or "").strip()
    if fixed and fixed != prompt:
        return fixed
    return None


def apply_keyframe_qc(
    db: Session, shot: Shot, prompt: str, *, with_llm: bool = True
) -> str:
    """出图前质检入口：硬错误直接阻断；其余交给 LLM 修正，修不了再阻断并给出可读原因。"""
    issues = check_keyframe_prompt(db, shot, prompt)
    hard = [i for i in issues if i["level"] == "error"]
    if hard:
        raise errors.AppError(
            "reference_asset_missing",
            "；".join(i["detail"] for i in hard) + "。请先生成缺失的道具锚定图后重试",
            409,
        )
    meta = {"stage": "keyframe", "issues": issues, "rewritten": False}
    if not issues:
        db.info["prompt_qc_last"] = meta
        return prompt
    if with_llm:
        fixed = llm_audit_keyframe_prompt(db, shot, prompt, issues)
        if fixed:
            meta.update({"rewritten": True, "original_prompt": prompt})
            db.info["prompt_qc_last"] = meta
            return fixed
    unresolved = [i["detail"] for i in issues]
    raise errors.AppError(
        "prompt_position_missing",
        "分镜图提示词站位不完整：" + "；".join(unresolved)
        + "。请补充每个角色的画面位置后重试",
        409,
    )


# ── 分镜视频提示词质检 ────────────────────────────────────────────
# 视频提示词在解析阶段生成，出视频时直接取库。解析时看不到真实资产与相邻关键帧，
# 因此出视频前再做一次一致性校验：确定性检查发现疑点才调 LLM，允许整段重写。

CLIP_QC_RULES_VERSION = 1
_CLIP_QC_CACHE: dict[str, tuple[str, dict[str, Any]]] = {}
_CLIP_QC_CACHE_MAX = 512

_FRAMING_CONFLICT_RE = re.compile(r"跑出画面|奔出画面|跑向远处|冲向远处|全身入画|走出画面")
_CLOSEUP_TOKENS = ("特写", "近景")


def _character_names(db: Session, project_id: str) -> set[str]:
    return {ch.name for ch in db.query(Character).filter(Character.project_id == project_id).all() if ch.name}


def _visual_text(prompt: str) -> str:
    """剔除「对白与声音」分段：对白里提到的角色/道具并不出现在画面，不能参与一致性比对。"""
    m = re.search(r"(?:^|\n)\s*对白与声音\s*[:：]", prompt)
    if m is None:
        return prompt
    tail = prompt[m.end():]
    nxt = re.search(r"\n\s*(输出|禁止|允许变化|必须保持|目标状态)\s*[:：]", tail)
    if nxt is not None:
        return prompt[: m.start()] + tail[nxt.start():]
    return prompt[: m.start()]


def check_clip_prompt(db: Session, shot: Shot, prompt: str) -> list[dict[str, str]]:
    """视频提示词确定性校验：与分镜图的一致性 + 首帧衔接 + 景别冲突。

    只产出可修复的 warn 级问题，由 LLM 统一重写；LLM 不可用时放行（不阻断出视频）。
    """
    scene = db.get(Scene, shot.scene_id)
    if scene is None or not prompt:
        return []
    issues: list[dict[str, str]] = []
    image_prompt = shot.image_prompt or ""
    allowed = _character_names(db, scene.project_id)
    visual = _visual_text(prompt)

    # 视频提示词是散文体，角色名通常不带 @，统一按名称出现与否比对
    image_chars = {n for n in allowed if n in image_prompt}
    clip_chars = {n for n in allowed if n in visual}
    for name in sorted(clip_chars - image_chars):
        issues.append({
            "code": "character_extra",
            "level": "warn",
            "detail": f"视频提示词出现分镜图中没有的角色『{name}』，可能导致角色数量变化",
        })
    for name in sorted(image_chars - clip_chars):
        issues.append({
            "code": "character_missing",
            "level": "warn",
            "detail": f"分镜图中的角色『{name}』未在视频提示词中出现，可能凭空消失",
        })

    for prop in _visible_props(db, shot, image_prompt):
        if prop.name and prop.name not in visual:
            issues.append({
                "code": "prop_missing",
                "level": "warn",
                "detail": f"分镜图中的道具『{prop.name}』未在视频提示词中出现，可能消失或变形",
            })

    if getattr(shot, "requires_prev_frame", False) and "衔接" not in prompt and "开场" not in prompt:
        issues.append({
            "code": "continuity_missing",
            "level": "warn",
            "detail": "该镜头依赖上一镜末帧，但视频提示词没有首帧衔接描述",
        })

    shot_type = shot.shot_type or ""
    if any(tok in shot_type for tok in _CLOSEUP_TOKENS) and _FRAMING_CONFLICT_RE.search(prompt):
        issues.append({
            "code": "framing_conflict",
            "level": "warn",
            "detail": f"景别为『{shot_type}』但提示词要求角色跑出/走出画面，动作幅度与景别冲突",
        })
    return issues


_CLIP_SYSTEM = """你是分镜视频提示词质检员。视频提示词用于把已确认的分镜图延续成动态镜头。给定镜头信息、分镜图提示词、资产清单与待修正问题，输出修正后的完整视频提示词。

检查维度：
1. 角色一致性：出场角色必须与分镜图完全一致，不得新增、删除、合并角色；
2. 道具连续性：分镜图中的道具必须出现，持有者与位置连续，不得凭空消失或复制；
3. 首帧衔接：依赖上一镜末帧时，开场状态必须描述如何接上；
4. 动作与景别：特写/近景不得出现跑出画面等全身大幅位移；
5. 时长与对白：保持原时长与对白文本不变。

只输出 JSON：{"ok": true/false, "issues": [{"code": "...", "detail": "..."}], "fixed_prompt": "完整视频提示词，无问题或无法修正时为空字符串"}。
允许整段重写，但必须保留：任务抬头、镜头编号、剧情与动作、对白、时长、参考图标签格式（如 <Picture 1>）。"""


def _clip_cache_key(shot: Shot, prompt: str) -> str:
    import hashlib

    raw = "\u0001".join([
        str(CLIP_QC_RULES_VERSION),
        shot.id,
        shot.image_prompt or "",
        prompt,
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def llm_audit_clip_prompt(
    db: Session, shot: Shot, prompt: str, issues: list[dict[str, str]]
) -> str | None:
    """LLM 复核并整段重写视频提示词；未启用/失败时返回 None。"""
    try:
        from app.integrations.llm import LLMClient

        scene = db.get(Scene, shot.scene_id)
        props = [
            {"name": p.name, "has_anchor": bool(p.anchor_asset_id)}
            for p in _visible_props(db, shot, shot.image_prompt or "")
        ]
        manifest = {
            "episode": shot.episode,
            "seq": shot.seq,
            "shot_type": shot.shot_type,
            "duration_seconds": getattr(shot, "duration", None),
            "requires_prev_frame": bool(getattr(shot, "requires_prev_frame", False)),
            "scene": scene.title if scene is not None else None,
            "props": props,
        }
        user = (
            f"镜头信息：{manifest}\n已知问题：{issues}\n\n"
            f"分镜图提示词：\n{shot.image_prompt or '（无）'}\n\n"
            f"待修正的视频提示词：\n{prompt}"
        )
        data: dict[str, Any] = LLMClient().chat_json(_CLIP_SYSTEM, user)
    except Exception as exc:  # noqa: BLE001 - LLM 异常不阻断出视频
        logger.warning("clip prompt_qc llm audit skipped: %s", exc)
        return None
    fixed = str(data.get("fixed_prompt") or "").strip()
    if not fixed or fixed == prompt:
        return None
    # 防止模型输出截断或跑题：长度不得低于原文一半，且必须保留任务抬头
    if len(fixed) < max(40, len(prompt) // 2) or "任务" not in fixed:
        logger.warning("clip prompt_qc llm rewrite rejected (len=%s)", len(fixed))
        return None
    return fixed


def apply_clip_qc(db: Session, shot: Shot, prompt: str, *, with_llm: bool = True) -> tuple[str, dict[str, Any]]:
    """出视频前质检：确定性疑点 → LLM 整段重写（带缓存）。返回 (提示词, 质检快照)。"""
    issues = check_clip_prompt(db, shot, prompt)
    meta: dict[str, Any] = {
        "stage": "clip",
        "rules_version": CLIP_QC_RULES_VERSION,
        "issues": issues,
        "rewritten": False,
    }
    if not issues:
        db.info["prompt_qc_last"] = meta
        return prompt, meta
    key = _clip_cache_key(shot, prompt)
    cached = _CLIP_QC_CACHE.get(key)
    if cached is not None:
        fixed, cached_meta = cached
        meta.update(cached_meta)
        db.info["prompt_qc_last"] = meta
        return fixed or prompt, meta
    fixed = llm_audit_clip_prompt(db, shot, prompt, issues) if with_llm else None
    result_meta = dict(meta)
    if fixed:
        result_meta.update({"rewritten": True, "original_prompt": prompt})
    else:
        result_meta["unresolved"] = [i["detail"] for i in issues]
    if len(_CLIP_QC_CACHE) >= _CLIP_QC_CACHE_MAX:
        _CLIP_QC_CACHE.clear()
    _CLIP_QC_CACHE[key] = (fixed or "", result_meta)
    meta.update(result_meta)
    db.info["prompt_qc_last"] = meta
    return fixed or prompt, meta
