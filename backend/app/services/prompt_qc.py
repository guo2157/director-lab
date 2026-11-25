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
from app.services.prompt_store import _register_default, get_prompt

logger = logging.getLogger(__name__)

_ROLE_LABEL = {"character": "角色定妆图", "scene": "场景基准图", "prop": "道具锚定图"}
_ROLE_KEEP = {
    "character": "仅用作该角色的面部、发型、体型与服装；保持{t}中的外形与配色不变",
    "scene": "仅用作背景的空间结构、地形、植被、光线方向与色调；不要把{t}缩小、平铺或原样放进画面",
    "prop": "仅用作道具的形状、颜色与材质，与{t}完全一致；画面中只出现一个道具、一个视角",
}

# 站位/空间词：命中任一即视为该角色已给出画面位置
_POSITION_TOKENS = (
    "画面左", "画面右", "左侧", "右侧", "左边", "右边", "偏左", "偏右", "居左", "居右",
    "中部", "中央", "正中", "前景", "背景", "上方", "下方", "角落", "边缘",
    "画左", "画右", "左上", "右上", "左下", "右下",
    # 场景相对站位（2026-11-25 补）：真实提示词大量使用「位于收银台后方」「窗边」
    # 「柜台后」这类以场景物件为锚点的位置描述，原词表只认画面相对方位（左/右/
    # 中央），于是镜 9「林小满：位于收银台后方」被判「未给出站位」→ LLM 改写或
    # 直接 409 阻断。实测该镜最终只画出 1 人（期望 2 人：林小满、陈默）。
    "后方", "前方", "内侧", "外侧", "一侧", "深处", "近处", "中景", "远景", "后景",
    "窗边", "门口", "入口", "台后", "柜台后", "收银台后", "吧台后", "桌后", "床侧",
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


def _position_sentences(text: str, name: str) -> list[str]:
    """取所有提到该角色的句子（裸名或 @引用），剔除「参考资产」清单句。

    原实现只认 ``@名称``，而站位句用的是裸名（「林小满：画面左侧中景，站在收银台
    后方」），于是镜 7/9 明明写清了站位仍被判 ``character_position_missing``：
    唯一命中的 ``@名称`` 句是提示词首行的「参考资产：@林小满 @陈默 @便利店」，
    那里当然没有位置词。误判代价是触发一次 LLM 改写（白烧一次请求），改写失败则
    409 阻断出图。
    """
    token = f"@{name}"
    out: list[str] = []
    for seg in re.split(r"[；;。\n]", text):
        if not seg.strip():
            continue
        if token not in seg and name not in seg:
            continue
        # 参考资产清单行只声明注入了哪些图，不含画面站位
        if "参考资产" in seg:
            continue
        out.append(seg)
    return out


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
                segs = _position_sentences(target or bounds_text, name)
                if not any(any(tok in seg for tok in _POSITION_TOKENS) for seg in segs):
                    issues.append(
                        {
                            "code": "character_position_missing",
                            "level": "warn",
                            "detail": f"角色『{name}』未在同一句内给出画面站位（左/右/中/前景等）",
                        }
                    )
    return issues


_SYSTEM = _register_default("qc_keyframe_prompt", """你是分镜图提示词质检员。给定镜头信息与最终提示词，检查两项：
1. 多角色站位：每个出场角色的画面位置是否明确且互不冲突（左右/前后/中景），是否可能被模型互换；
2. 道具完整性：本镜可见道具是否都在参考图职责中出现并绑定图序，是否存在只写文字没有参考图的道具。
只输出 JSON：{"ok": true/false, "issues": [{"code": "...", "detail": "..."}], "fixed_prompt": "修正后的完整提示词，无问题或无法修正时为空字符串"}。
允许整段重写以彻底修正站位与道具引用，但必须保留：剧情内容、美术风格、必须保持/允许变化/禁止分段、参考图的图序与标签格式；不得新增或删除角色、道具。""")


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
        data: dict[str, Any] = LLMClient().chat_json(get_prompt("qc_keyframe_prompt"), user)
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
    m = re.search(r"(?:^|\n)\s*【?对白与声音】?\s*[:：]?", prompt)
    if m is None:
        return prompt
    tail = prompt[m.end():]
    nxt = re.search(r"\n\s*(?:【|输出|禁止|允许变化|必须保持|目标状态)\s*", tail)
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
    # 道具数量/持有关系是结构化冲突，不能交给 LLM 改文案掩盖，必须阻断提交
    issues.extend(check_prop_count_consistency(db, shot))
    return issues


# ── 道具数量 / 持有关系确定性校验 ──────────────────────────────
# 分镜结构化数据里 holding 仍是自然语言（DATABASE.md：六项枚举均为文本），没有
# 数量/左右手字段。这里只做保守的规则解析：仅当「开场数量、交接数量、目标数量」
# 三侧都能明确解析时才判定冲突；任一侧无法解析即跳过（宁可漏报，不做猜测式阻断）。
# 判定口径与剧情一致：交接不改变道具总量，只改变持有者与所在手。

PROP_COUNT_QC_VERSION = 1

_CN_DIGITS = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "百": 100, "半": 0.5,
}
_UNIT_CHARS = "只杯个瓶罐支把块张份袋盒盘碗"
_QTY_RE = re.compile(
    rf"(?P<num>\d+(?:\.\d+)?|[零一二两三四五六七八九十百半]+)\s*"
    rf"(?P<unit>[{_UNIT_CHARS}])(?P<rest>[^，,。；;、：:\n]{{0,16}})"
)
_NOUN_STOP = re.compile(
    r"递给|交给|送给|拿给|递到|递出|接过|接住|收下|取出|放在|放到|塞给|"
    r"拿起|端起|举起|捧着|抱着|握着|托着|垂在|握|捧|抱|举|端|托|拿|放|递|接|取|收|持|的"
)
_NOUN_PREFIX = ("仍然", "仍", "还", "剩下", "剩余", "一共", "共", "各", "已", "现", "手中", "手里")
_GENERIC_NOUN = "__held__"
_EMPTY_HAND = "__empty_hand__"
_EMPTY_HAND_RE = re.compile(r"空手|空着|没有拿|未持|无物|两手空空")
_BOTH_RE = re.compile(r"双手各|两手各|左右手各|双手分别|两手分别|左右手分别")
_TRANSFER_OUT_RE = re.compile(r"递给|递出|递到|交给|交到|送给|送到|拿给|塞给")
_TRANSFER_IN_RE = re.compile(r"接过|接住|收下|取过")
_PICK_RE = re.compile(r"拿起|端起|拾起|捡起|取出")
_LOSS_RE = re.compile(r"放下|丢掉|扔下|吃掉|喝掉|消耗|销毁|摔碎")
_HAND_RE = re.compile(r"(左|右)手")
_HAND_SIDE = {"左": "left", "右": "right"}


def _parse_number(raw: str) -> float | None:
    """解析阿拉伯数字或 1~99 的中文数字（含半）。"""
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    total = 0
    section = 0
    num = 0
    for ch in raw:
        value = _CN_DIGITS.get(ch)
        if value is None:
            return None
        if value == 10:
            section += (num or 1) * 10
            num = 0
        elif value == 100:
            section += (num or 1) * 100
            num = 0
        elif value == 0.5 and not num and not section:
            num = 0.5
        else:
            num = value
    return section + num


def _normalize_noun(rest: str) -> str:
    """从数量后面的残余文本里提取道具名（如「一只冰可乐杯抱在胸前」→「冰可乐」）。"""
    text = _NOUN_STOP.split(rest or "")[0].strip()
    for prefix in _NOUN_PREFIX:
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    text = text.strip("的 ")
    while text and text[-1] in _UNIT_CHARS + "子":
        text = text[:-1]
    return text or _GENERIC_NOUN


def _clauses(text: str) -> list[str]:
    return [c for c in re.split(r"[，,。；;、\n]", text or "") if c.strip()]


def _holding_counts(text: str) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """解析 holding 文本：返回 (每个道具总数, {左手/右手: 每个道具数量})。

    「双手各持一杯」按每手一杯计数（合计 2）；「双手捧着同一只杯」合计 1；
    「双手握杆」没有数量单位，不产出计数（视为无法判定）。
    """
    total: dict[str, float] = {}
    hands: dict[str, dict[str, float]] = {"left": {}, "right": {}}
    for clause in _clauses(text):
        each = bool(_BOTH_RE.search(clause))
        shared = each or "双手" in clause or "两手" in clause
        hand = "left" if "左手" in clause else ("right" if "右手" in clause else None)
        matched = False
        for m in _QTY_RE.finditer(clause):
            value = _parse_number(m.group("num"))
            if value is None or value == 0:
                continue
            matched = True
            noun = _normalize_noun(m.group("rest"))
            amount = value * 2 if each else value
            total[noun] = total.get(noun, 0) + amount
            if each and hand is None:
                # 双手各持：左右手各一份
                hands["left"][noun] = hands["left"].get(noun, 0) + value
                hands["right"][noun] = hands["right"].get(noun, 0) + value
            elif hand is not None:
                hands[hand][noun] = hands[hand].get(noun, 0) + amount
            elif shared:
                # 双手共同捧持且未分左右：只记总量，不参与左右手比对
                continue
        if not matched and _EMPTY_HAND_RE.search(clause):
            # 明确写「空手 / 右手空着」：记为 0，便于校验接收方开局为空
            if hand is not None:
                hands[hand][_EMPTY_HAND] = 0
            else:
                total[_EMPTY_HAND] = 0
    return total, hands


def _split_holding_by_character(text: str, names: list[str]) -> dict[str | None, str]:
    """按角色名把 holding 文本切成逐角色片段；没有角色名时整段归入 None。"""
    if not text:
        return {}
    hits = sorted((text.find(n), n) for n in names if n and n in text)
    if not hits:
        return {None: text}
    result: dict[str | None, str] = {}
    for index, (start, name) in enumerate(hits):
        end = hits[index + 1][0] if index + 1 < len(hits) else len(text)
        result[name] = (result.get(name, "") + text[start:end]).strip()
    return result


def _state_holding(state: Any, names: list[str]) -> dict[str | None, tuple[dict[str, float], dict[str, dict[str, float]]]]:
    """从 start_state / end_state 取 holding 并解析为逐角色数量。"""
    if not isinstance(state, dict):
        return {}
    raw = state.get("holding")
    if raw is None:
        return {}
    if isinstance(raw, dict):
        pieces = {str(k): str(v) for k, v in raw.items() if str(v).strip()}
    else:
        pieces = _split_holding_by_character(str(raw), names)
    return {key: _holding_counts(text) for key, text in pieces.items() if text.strip()}


def _match_noun(mapping: dict[str, float], noun: str) -> str | None:
    """在解析结果中定位同一个道具名（支持「冰可乐」与「冰可乐杯」这类写法差异）。"""
    if not mapping:
        return None
    if noun in mapping:
        return noun
    if noun == _GENERIC_NOUN and len(mapping) == 1:
        return next(iter(mapping))
    candidates = [
        key for key in mapping
        if key != _GENERIC_NOUN and noun != _GENERIC_NOUN and (noun in key or key in noun)
    ]
    if not candidates:
        return None
    return max(candidates, key=len)


def _qty_near(text: str) -> tuple[float, str] | None:
    """取文本中唯一的「数量 + 道具」；多重歧义时返回 None。"""
    pairs: list[tuple[float, str]] = []
    for m in _QTY_RE.finditer(text or ""):
        value = _parse_number(m.group("num"))
        if value is None or value == 0:
            continue
        noun = _normalize_noun(m.group("rest"))
        if (value, noun) not in pairs:
            pairs.append((value, noun))
    if len(pairs) != 1:
        return None
    return pairs[0]


def _beat_events(action: str, names: list[str], default_actor: str | None) -> list[dict]:
    """从一拍动作里解析交接/拾取/失去事件（只解析能确定对象的简单句式）。"""
    events: list[dict] = []
    text = action or ""
    if not text:
        return events
    actor = next((n for n in names if n in text), None) or default_actor
    specs = (
        (_TRANSFER_OUT_RE, "transfer"),
        (_TRANSFER_IN_RE, "receive"),
        (_PICK_RE, "pick"),
        (_LOSS_RE, "loss"),
    )
    for regex, kind in specs:
        for m in regex.finditer(text):
            before, after = text[: m.start()], text[m.end():]
            before_names = [n for n in names if n in before]
            after_names = [n for n in names if n in after]
            source = target = None
            source_hand = target_hand = None
            if kind == "transfer":
                source = before_names[-1] if before_names else actor
                target = after_names[0] if after_names else None
                hand_before = _HAND_RE.findall(before)
                hand_after = _HAND_RE.findall(after)
                source_hand = _HAND_SIDE.get(hand_before[-1]) if hand_before else None
                target_hand = _HAND_SIDE.get(hand_after[0]) if hand_after else None
            elif kind == "receive":
                target = before_names[-1] if before_names else actor
                source = after_names[0] if after_names else None
                hand_before = _HAND_RE.findall(before)
                hand_after = _HAND_RE.findall(after)
                target_hand = _HAND_SIDE.get(hand_before[-1]) if hand_before else None
                source_hand = _HAND_SIDE.get(hand_after[0]) if hand_after else None
            elif kind == "pick":
                target = actor
            else:
                source = actor
            qty_pair = _qty_near(text)
            events.append({
                "kind": kind,
                "source": source,
                "target": target,
                "source_hand": source_hand,
                "target_hand": target_hand,
                "quantity": qty_pair[0] if qty_pair else None,
                "noun": qty_pair[1] if qty_pair else None,
                "action": text,
            })
    return events


def _prop_names(db: Session, project_id: str) -> list[str]:
    return [p.name for p in db.query(Prop).filter(Prop.project_id == project_id).all() if p.name]


def _matches_prop(noun: str, props: set[str]) -> bool:
    if noun == _GENERIC_NOUN:
        return False
    return any(noun == name or noun in name or name in noun for name in props)


def check_prop_count_consistency(db: Session, shot: Shot) -> list[dict[str, str]]:
    """道具数量/持有关系确定性校验（PROP_COUNT_CONFLICT）。

    校验链路：开场 holding 数量 → beats 中的交接/拾取/失去数量 → 目标 holding 数量。
    交接只转移持有者与所在手，不改变总量；数量变化必须由明确事件解释。
    """
    scene = db.get(Scene, shot.scene_id)
    if scene is None:
        return []
    names = _character_names(db, scene.project_id)
    start_by_char = _state_holding(shot.start_state, sorted(names))
    end_by_char = _state_holding(shot.end_state, sorted(names))
    if not start_by_char:
        return []

    def _char_total(side: dict, char: str | None, noun: str):
        piece = side.get(char)
        if piece is None and None in side and len(side) == 1:
            piece = side[None]
        if piece is None:
            return None
        mapping = piece[0]
        key = _match_noun(mapping, noun)
        if key is None:
            return 0.0 if mapping and all(k == _EMPTY_HAND for k in mapping) else None
        return mapping[key]

    def _char_hand(side: dict, char: str | None, hand: str | None, noun: str):
        if hand not in ("left", "right"):
            return None
        piece = side.get(char)
        if piece is None and None in side and len(side) == 1:
            piece = side[None]
        if piece is None:
            return None
        hand_map = piece[1].get(hand) or {}
        key = _match_noun(hand_map, noun)
        if key is None:
            return 0.0 if hand_map and all(k == _EMPTY_HAND for k in hand_map) else None
        return hand_map[key]

    default_actor = None
    if shot.character_id and len(start_by_char) == 1:
        ch = db.get(Character, shot.character_id)
        if ch is not None:
            default_actor = ch.name

    issues: list[dict[str, str]] = []
    seen: set[str] = set()
    events: list[dict] = []
    # P1 聚合改造：通过 shot_timeline() 访问器读取，旧数据自动回退 beats
    from app.services.shot_timeline import shot_timeline as _tl
    for unit in _tl(shot):
        action = unit.get("action") if isinstance(unit, dict) else str(unit)
        events.extend(_beat_events(str(action or ""), sorted(names), default_actor))

    props = set(_prop_names(db, scene.project_id))
    for event in events:
        qty, noun = event.get("quantity"), event.get("noun")
        if not qty or not noun:
            continue
        for side, char, sign in ((start_by_char, event.get("source"), -1),
                                 (start_by_char, event.get("target"), +1)):
            if char is None:
                continue
            start_total = _char_total(side, char, noun)
            if start_total is None:
                continue
            expected = start_total + sign * qty
            if expected < 0:
                detail = (
                    f"道具『{noun}』数量冲突：{char} 开场持有 {start_total:g}，"
                    f"但动作要求{'递出' if sign < 0 else '接收'} {qty:g}，持有量不足"
                )
                if detail not in seen:
                    seen.add(detail)
                    issues.append({"code": "PROP_COUNT_CONFLICT", "level": "error", "detail": detail})
                continue
            end_total = _char_total(end_by_char, char, noun)
            if end_total is not None and end_total != expected:
                detail = (
                    f"道具『{noun}』数量冲突：{char} 开场持有 {start_total:g}，"
                    f"动作{'递出' if sign < 0 else '接收'} {qty:g} 后应为 {expected:g}，"
                    f"但目标状态写的是 {end_total:g}"
                )
                if detail not in seen:
                    seen.add(detail)
                    issues.append({"code": "PROP_COUNT_CONFLICT", "level": "error", "detail": detail})
            # 左右手级别校验：仅当动作、开场、目标三处都明确到同一只手时可判定
            hand = event.get("source_hand" if sign < 0 else "target_hand")
            hand_label = {"left": "左手", "right": "右手"}.get(hand or "", "")
            start_hand = _char_hand(start_by_char, char, hand, noun)
            end_hand = _char_hand(end_by_char, char, hand, noun)
            if start_hand is None or end_hand is None:
                continue
            expected_hand = start_hand + sign * qty
            if expected_hand < 0 or end_hand == expected_hand:
                continue
            detail = (
                f"道具『{noun}』左右手归属冲突：{char} {hand_label}开场持有 {start_hand:g}，"
                f"动作{'递出' if sign < 0 else '接收'} {qty:g} 后应为 {expected_hand:g}，"
                f"但目标状态写的是 {end_hand:g}"
            )
            if detail not in seen:
                seen.add(detail)
                issues.append({"code": "PROP_COUNT_CONFLICT", "level": "error", "detail": detail})

    # 无任何数量事件时，数量不允许自行变化（仅对项目内已登记道具判定，避免把环境物当道具）
    if not any(e.get("quantity") for e in events):
        for char, (start_total, _start_hands) in start_by_char.items():
            end_piece = end_by_char.get(char)
            if end_piece is None:
                continue
            end_total = end_piece[0]
            for noun, amount in start_total.items():
                if not _matches_prop(noun, props):
                    continue
                key = _match_noun(end_total, noun)
                if key is None or end_total[key] == amount:
                    continue
                detail = (
                    f"道具『{noun}』数量冲突：开场持有 {amount:g}，目标状态为 "
                    f"{end_total[key]:g}，但动作时间线没有任何交接/拾取/消耗事件"
                )
                if detail not in seen:
                    seen.add(detail)
                    issues.append({"code": "PROP_COUNT_CONFLICT", "level": "error", "detail": detail})
    return issues


def check_prev_state_continuity(db: Session, shot: Shot) -> list[str]:
    """下一镜开场状态与上一镜末态的文本对账（warn 级，不阻断）。

    尾帧是图片，无法直接读取画面中的道具数量；这里以上一镜 end_state 的文本作为
    「实际尾帧状态」代理，发现数量/持有关系不一致时提示人工确认，而不是静默放行。
    """
    from app.services.shot_chain import previous_shot

    prev = previous_shot(db, shot)
    if prev is None or prev.scene_id != shot.scene_id or not prev.last_frame_asset_id:
        return []
    scene = db.get(Scene, shot.scene_id)
    if scene is None:
        return []
    names = sorted(_character_names(db, scene.project_id))
    prev_end = _state_holding(prev.end_state, names)
    cur_start = _state_holding(shot.start_state, names)
    if not prev_end or not cur_start:
        return []
    props = set(_prop_names(db, scene.project_id))
    warnings: list[str] = []
    for char, (prev_total, _hands) in prev_end.items():
        cur_piece = cur_start.get(char) or (cur_start.get(None) if None in cur_start else None)
        if cur_piece is None:
            continue
        cur_total = cur_piece[0]
        for noun, amount in prev_total.items():
            if not _matches_prop(noun, props):
                continue
            key = _match_noun(cur_total, noun)
            if key is None:
                warnings.append(
                    f"上一镜末帧中『{noun}』数量为 {amount:g}，本镜开场状态未提及该道具，"
                    f"请确认是否被遗漏或已在剧情中放下"
                )
            elif cur_total[key] != amount:
                warnings.append(
                    f"上一镜末帧中『{noun}』数量为 {amount:g}，本镜开场为 {cur_total[key]:g}，"
                    f"疑似跨镜连续性不一致，请人工确认"
                )
    # 相同道具/数量只提示一次
    return list(dict.fromkeys(warnings))


# ── 提示词结构检查（warn 级，不阻断）────────────────────────────
# 手工维护的长模板经常留下空标题或断段（实测第 1 集第 5 镜出现空的「【七、」），
# 这类残缺不会直接导致画面错误，但会让后续模型误判分段职责，先提示再人工/LLM 修。
_NUMBERED_HEADING_RE = re.compile(r"^([一二三四五六七八九十]+)、")
_HEADING_RE = re.compile(r"^\s*【([^】]{0,40})(】?)")


def check_prompt_structure(prompt: str) -> list[str]:
    """检查编号分段标题是否残缺或为空；只返回提示，不参与阻断。"""
    warnings: list[str] = []
    lines = (prompt or "").split("\n")
    for index, line in enumerate(lines):
        match = _HEADING_RE.match(line)
        if not match:
            continue
        title = match.group(1).strip()
        if not _NUMBERED_HEADING_RE.match(title):
            continue
        if not match.group(2):
            warnings.append(f"分段标题『{line.strip()[:24]}』缺少结束括号或标题被截断")
            continue
        cursor = index + 1
        while cursor < len(lines) and not lines[cursor].strip():
            cursor += 1
        if cursor >= len(lines) or _HEADING_RE.match(lines[cursor]):
            warnings.append(f"分段『【{title}】』没有任何内容")
    return list(dict.fromkeys(warnings))


_CLIP_SYSTEM = _register_default("qc_clip_prompt", """你是分镜视频提示词质检员。视频提示词用于把已确认的分镜图延续成动态镜头。给定镜头信息、分镜图提示词、资产清单与待修正问题，输出修正后的完整视频提示词。

检查维度：
1. 角色一致性：出场角色必须与分镜图完全一致，不得新增、删除、合并角色；
2. 道具连续性：分镜图中的道具必须出现，持有者与位置连续，不得凭空消失或复制；
3. 首帧衔接：依赖上一镜末帧时，开场状态必须描述如何接上；
4. 动作与景别：特写/近景不得出现跑出画面等全身大幅位移；
5. 时长与对白：保持原时长与对白文本不变；「对白与声音」段只保留 `<d>[Chinese] 台词原文</d>` 形式，禁止出现“台词：”“对白：”“第N段”等标签，环境声/音乐描述留在 <d> 外。

只输出 JSON：{"ok": true/false, "issues": [{"code": "...", "detail": "..."}], "fixed_prompt": "完整视频提示词，无问题或无法修正时为空字符串"}。
允许整段重写，但必须保留：任务抬头、镜头编号、剧情与动作、对白、时长、参考图标签格式（如 <Picture 1>）。""")


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
        data: dict[str, Any] = LLMClient().chat_json(get_prompt("qc_clip_prompt"), user)
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
    """出视频前质检：确定性疑点 → LLM 整段重写（带缓存）。返回 (提示词, 质检快照)。

    道具数量/持有关系冲突（PROP_COUNT_CONFLICT）属于结构化错误：
    改文案无法修复，直接阻断提交并给出可定位到镜头字段的原因。
    """
    issues = check_clip_prompt(db, shot, prompt)
    meta: dict[str, Any] = {
        "stage": "clip",
        "rules_version": CLIP_QC_RULES_VERSION,
        "prop_count_rules_version": PROP_COUNT_QC_VERSION,
        "issues": issues,
        "continuity_warnings": check_prev_state_continuity(db, shot),
        "structure_warnings": check_prompt_structure(prompt),
        "rewritten": False,
    }
    hard = [i for i in issues if i.get("level") == "error"]
    if hard:
        db.info["prompt_qc_last"] = meta
        raise errors.AppError(
            "PROP_COUNT_CONFLICT",
            "；".join(i["detail"] for i in hard)
            + "。请先修正分镜的开场状态、动作时间线与目标状态后重试",
            409,
            issues=hard,
        )
    if not issues:
        db.info["prompt_qc_last"] = meta
        return prompt, meta
    # 已是 H3 三段式的提示词（通常是用户在详情页手工编辑的）禁止整段重写：
    # LLM 重写会丢掉 overall_soundscape/non_diegetic_music 并改动台词，
    # 等于用旧值覆盖用户本次提交的内容；格式问题由 to_h3_clip_prompt 收敛
    if "integrated_multimodal_description:" in (prompt or ""):
        meta["unresolved"] = [i["detail"] for i in issues]
        meta["rewrite_skipped"] = "h3_prompt_protected"
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
