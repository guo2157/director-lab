"""对话内生成申请的目标定位（生成能力接入对话）。
设计约束（AGENTS.md P0）：不猜用户意图对象。
- 镜头目标：只在消息明确给出镜头标识时定位：sht_ 前缀的镜头 ID，或『第 N 镜』
  - 『第 N 镜』在项目内必须唯一命中；命中多个视为无法确定，交澄清而非挑一个
- 实体目标（角色/场景/道具）：只接受明确的实体 ID（chr_/scn_/prp_ 前缀），
  不按名称猜测（同名/近名实体会导致生成错对象，浪费算力与成本）
- 定位不到一律不创建申请，避免生成错目标造成算力与成本浪费
- 生成目标（关键帧/视频）由关键词判定，判定不出时默认关键帧（最常用且成本最低）
"""
import re
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.models import Character, Prop, Scene, Shot

# 镜头 ID：与项目内 ID 规则一致（sht_ + 小写字母数字）
_SHOT_ID_RE = re.compile(r"\bsht_[a-z0-9]{4,}\b")
# 『第 3 镜』『第3个镜头』等常见表述
_SHOT_SEQ_RE = re.compile(r"第\s*(\d+)\s*(?:个)?\s*(?:镜|镜头)")
# 实体 ID 前缀 → 实体目标（只认 ID，不按名称猜）
_ENTITY_ID_RES = (
    (re.compile(r"\bchr_[a-z0-9]{4,}\b"), "character"),
    (re.compile(r"\bscn_[a-z0-9]{4,}\b"), "scene"),
    (re.compile(r"\bprp_[a-z0-9]{4,}\b"), "prop"),
)
_ENTITY_MODELS = {"character": Character, "scene": Scene, "prop": Prop}

# 生成目标判定：先判音频（最具体），再判视频，再判图片类，都未命中默认关键帧
_AUDIO_HINTS = ("配音", "语音", "台词", "朗读", "tts", "TTS", "旁白")
_CLIP_HINTS = ("视频", "片段", "动态")
_KEYFRAME_HINTS = ("关键帧", "分镜图", "静图", "首帧", "图片", "出图")


def resolve_target(text: str) -> str:
    """从消息判定生成目标：audio / clip / keyframe。判定不出时取成本最低的关键帧。"""
    if any(k in text for k in _AUDIO_HINTS):
        return "audio"
    if any(k in text for k in _CLIP_HINTS):
        return "clip"
    if any(k in text for k in _KEYFRAME_HINTS):
        return "keyframe"
    return "keyframe"


# 随手生成（A 方案）：从「帮我生成一只小狗的图片」这类请求里剥离命令词，留下主体当提示词
_IMAGE_CMD_PREFIX_RE = re.compile(
    r"^\s*(?:(?:请|帮我|帮忙|麻烦|给我|替我|我想|我要|想要|需要|再))*\s*"
    r"(?:生成|画|绘制|出|做|来)\s*(?:一(?:张|幅|个|只|条|位|段)|[张幅个只条])?\s*"
)
_IMAGE_NOUN_SUFFIX_RE = re.compile(r"\s*(?:的)?(?:图片|图像|插画|照片|壁纸|图)\s*$")
# 分镜/镜头相关请求依赖项目上下文（分镜提示词、参考图、阶段门禁），不能随手出图
_SHOT_SCOPED_RE = re.compile(
    r"(?:分镜|镜头|镜号|第\s*\d+\s*镜|本镜|该镜|这镜|关键帧|首帧|尾帧|画面)"
)
_IMAGE_NOUN_HINTS = ("图片", "图像", "插画", "海报", "照片", "壁纸", "图")
_IMAGE_QUANTIFIER_RE = re.compile(r"一(?:张|幅)|[张幅]")
_IMAGE_VERB_RE = re.compile(r"(?:画|绘制)")


def extract_image_prompt(text: str) -> str:
    """从明确的出图请求中提取提示词主体；不是「随手出图」请求时返回空串。

    只有满足以下条件才视为随手出图：
    - 不含镜头/分镜/关键帧等依赖项目上下文的词；
    - 含图片类名词（图片/插画/海报…）、图片量词（一张/一幅）或作画动词（画/绘制）。

    提取只做机械剥离（命令词 / 量词 / 结尾名词），不做语义改写；
    返回空串说明请求过于笼统或需要项目上下文，由调用方走原有引导。
    """
    t = (text or "").strip()
    if not t or _SHOT_SCOPED_RE.search(t):
        return ""
    if not (
        any(k in t for k in _IMAGE_NOUN_HINTS)
        or _IMAGE_QUANTIFIER_RE.search(t)
        or _IMAGE_VERB_RE.search(t)
    ):
        return ""
    t = _IMAGE_CMD_PREFIX_RE.sub("", t)
    t = _IMAGE_NOUN_SUFFIX_RE.sub("", t)
    return t.strip(" ，。,.!！?？~～、:：")


def resolve_shot(db: Session, project_id: str, text: str) -> tuple[Shot | None, str | None]:
    """定位消息所指镜头。
    返回 (shot, None) 表示唯一定位成功；
    返回 (None, reason) 表示无法定位，reason 取值：
    - shot_unspecified：消息未给出可识别的镜头标识
    - shot_not_found：给出的标识在库中不存在
    - shot_not_in_project：镜头存在但不属于当前项目
    - shot_ambiguous：『第 N 镜』在项目内命中多个，无法确定
    """
    matched = _SHOT_ID_RE.search(text or "")
    if matched:
        shot = db.get(Shot, matched.group(0))
        if shot is None:
            return None, "shot_not_found"
        scene = db.get(Scene, shot.scene_id)
        if scene is None or scene.project_id != project_id:
            return None, "shot_not_in_project"
        return shot, None
    seq_match = _SHOT_SEQ_RE.search(text or "")
    if seq_match:
        seq = int(seq_match.group(1))
        shots = list(
            db.scalars(
                select(Shot)
                .join(Scene, Shot.scene_id == Scene.id)
                .where(Scene.project_id == project_id, Shot.seq == seq)
            ).all()
        )
        if not shots:
            return None, "shot_not_found"
        if len(shots) > 1:
            return None, "shot_ambiguous"
        return shots[0], None
    return None, "shot_unspecified"


def resolve_entity(
    db: Session, project_id: str, text: str
) -> tuple[object | None, str | None, str | None]:
    """定位消息所指的项目级实体（角色/场景/道具）。
    只接受明确的实体 ID（chr_/scn_/prp_ 前缀），不按名称猜测：
    名称匹配在同名实体、近名实体场景下会生成错对象。
    返回 (entity, target, None) 表示唯一定位成功，target 为 character/scene/prop；
    返回 (None, None, reason) 表示无法定位，reason 取值：
    - entity_unspecified：消息未给出可识别的实体 ID
    - entity_not_found：给出的 ID 在库中不存在
    - entity_not_in_project：实体存在但不属于当前项目
    """
    for id_re, target in _ENTITY_ID_RES:
        matched = id_re.search(text or "")
        if matched:
            model = _ENTITY_MODELS[target]
            entity = db.get(model, matched.group(0))
            if entity is None:
                return None, None, "entity_not_found"
            if entity.project_id != project_id:
                return None, None, "entity_not_in_project"
            return entity, target, None
    return None, None, "entity_unspecified"
