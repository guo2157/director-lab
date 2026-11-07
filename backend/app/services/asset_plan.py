"""资产定义生成：解析落库后一次性定稿名称、占位路径、提示词与参考依赖。

约定（本模块为命名的唯一权威来源）：
- 名称固定：{类型前缀}_{语义片段}_{8 位短哈希}，一经生成永不修改；
  分镜图/分镜视频额外带 集/场景/镜头 序号，便于人工核对。
- 路径固定：data/media/plans/{project_id}/{asset_key}.png|mp4，生成前即确定。
- 提示词：公共资产写实描述；分镜图/分镜视频按模板引用已定稿的参考资产名。
- 状态：pending 待生成，成功产出后由生成环节回写 generated 与 asset_id。
"""
import hashlib
import logging
import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import ids
from app.models.asset_plan import AssetPlan, AssetPlanDependency
from app.models.narrative import Character, Prop, Scene, Shot

logger = logging.getLogger(__name__)

PLAN_PREFIX = {"character": "chr", "scene": "scn", "prop": "prp", "keyframe": "kf", "clip": "clip"}
PLAN_EXT = {"character": ".png", "scene": ".png", "prop": ".png", "keyframe": ".png", "clip": ".mp4"}
# 分镜图槽位：角色身份 / 服装 / 场景 / 道具
KEYFRAME_SLOTS = ("ref_identity", "ref_character_2", "ref_scene", "ref_prop", "ref_character_3", "ref_character_4", "ref_character_5", "ref_character_6")
# 分镜视频槽位：当前分镜图 / 角色 / 场景 / 上一镜末帧
CLIP_SLOTS = ("ref_image", "ref_identity", "ref_character_2", "ref_prop", "ref_prev_frame", "ref_character_3", "ref_character_4", "ref_character_5", "ref_character_6")


def _slug(text: str) -> str:
    s = re.sub(r"[^0-9a-zA-Z]+", "_", text or "").strip("_").lower()
    return s[:24]


def _short(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:8]


def make_asset_key(plan_type: str, name: str, salt: str, seq: int | None = None) -> str:
    """生成固定唯一名称；salt 用于稳定区分同项目内同名实体。"""
    prefix = PLAN_PREFIX[plan_type]
    slug = _slug(name)
    digest = _short(f"{salt}|{name}|{plan_type}")
    if seq is not None:
        return f"{prefix}_{slug}_{seq:03d}_{digest}" if slug else f"{prefix}_{seq:03d}_{digest}"
    return f"{prefix}_{slug}_{digest}" if slug else f"{prefix}_{digest}"


def make_media_path(project_id: str, asset_key: str, plan_type: str) -> str:
    root = Path(get_settings().media_dir) / "plans" / project_id
    return str(root / f"{asset_key}{PLAN_EXT[plan_type]}")


def _character_prompt(c: Character) -> str:
    parts = [c.name]
    if c.appearance:
        parts.append(c.appearance)
    if c.description:
        parts.append(c.description)
    parts.append("人物定妆图，正面全身，中性表情，纯色背景，高清写实，保持人物身份特征稳定")
    return "，".join(p for p in parts if p)


# 场景描述清洗：剧情动作谓语命中即整句剔除（cfg=1 下负向词无效，只能靠正向改写消除主体）
_SCENE_STORY_VERBS = (
    "喊|叫|哭|笑|跑|奔|赶|扑|追|道歉|承认|拒绝|警告|劝|说|回答|"
    "发呆|无聊|戏弄|恶作剧|惊|四散|旁白|总结|点明|"
    "坐|站|走|卧|躺|跪|俯|仰|抱|握|挥|推|拉|递|拿|放|打开|关上|"
    "吃|喝|读|看|望|听|启|服毒|自尽|杀|打|踢|唤|引|带"
)


def _clean_scene_desc(text: str, char_names: list[str]) -> str:
    """清除非环境内容：角色名、动作谓语与剧情因果从句一律剔除，只留空间与光线描述。"""
    if not text:
        return ""
    names = [n for n in (char_names or []) if n]
    clauses = re.split(r"([，。；、])", text)
    kept: list[str] = []
    buf = ""
    for part in clauses:
        if part in ("，", "。", "；", "、"):
            buf += part
            continue
        seg = buf + part
        buf = ""
        s = seg.strip()
        if not s:
            continue
        if any(n in s for n in names):
            continue
        if re.search(_SCENE_STORY_VERBS, s):
            continue
        kept.append(seg)
    out = "".join(kept)
    out = re.sub(r"[，、；]{2,}", "，", out)
    out = re.sub(r"^[，、。；]+|[，、。；]+$", "", out).strip()
    return out


def _scene_prompt(s: Scene, char_names: list[str] | None = None) -> str:
    """场景基准图提示词：空闲场景，只描述环境。

    优先使用场景板（plate_text）产出的空镜规格；没有场景板时清洗剧本描述，
    剔除角色名、动作与剧情从句，仅保留地形、时间与光照等空间信息，不虚构内容。
    """
    names = [n for n in (char_names or []) if n]
    parts: list[str] = []
    title = s.title or ""
    if title and not any(n in title for n in names):
        parts.append(title)
    plate = (getattr(s, "plate_text", None) or "").strip()
    if plate:
        # 只取前 3 行：第四行是风格，模板已通过 {style} 单独注入，重复会打架。
        # 各行自带句号，而下方以「，」连接，故先剥掉行尾标点，避免拼出「。，」。
        lines = [
            l.strip().rstrip("。.；;，,")
            for l in plate.splitlines() if l.strip()
        ][:3]
        parts.extend(lines)
    else:
        cleaned = _clean_scene_desc(s.description or "", names)
        if cleaned:
            parts.append(cleaned)
        elif s.description:
            first = re.split(r"[，。；]", s.description)[0].strip()
            if first and not any(n in first for n in names):
                parts.append(first)
    if s.time_note:
        parts.append(s.time_note)
    parts.append(
        "空场景基准图，广角全景，高清写实，画面只由该场景的固定环境元素与天空构成，"
        "保持空间结构与光线稳定"
    )
    return "，".join(p for p in parts if p)


def _prop_prompt(p: Prop) -> str:
    parts = [p.name]
    if p.appearance:
        parts.append(p.appearance)
    if p.description:
        parts.append(p.description)
    parts.append("道具锚定图，单件居中，纯色背景，高清写实，保持材质与外形稳定")
    return "，".join(x for x in parts if x)


def _ref_line(label: str, key: str, name: str) -> str:
    return f"{label} {key}（{name}）"


def _keyframe_prompt(shot: Shot, char_key: str | None, char_name: str | None,
                     scene_key: str, scene_title: str) -> str:
    # 引用统一用 @名称 书写，生成环节的 @引用解析才能携带对应参考图；
    # 槽位顺序由引用类型分组决定（角色 ref_identity→ref_character_2，场景 ref_scene，
    # 道具 ref_prop），与文本先后无关，此处先场景后角色仅为可读性。
    refs = [f"参考场景 @{scene_title}"] if scene_title else []
    if char_name:
        refs.append(f"参考角色 @{char_name}")
    head = "；".join(refs)
    # 角色身份绑定与隔离规则（P1）：明确每个 @角色 的身份参考来源与禁止特征继承，
    # 降低多角色分镜中第二角色继承第一角色面部/服装特征的概率
    identity_rules: list[str] = []
    if char_name:
        identity_rules.append(
            f"角色身份绑定：@{char_name} 的定妆图是其唯一身份参考依据，"
            f"画面中该角色必须严格保持定妆图中的面部、毛色/发型、体型与服装特征，"
            "不得继承或混入其他角色的外观特征"
        )
    if scene_title:
        identity_rules.append(
            f"场景一致性：@{scene_title} 的基准图是环境唯一参考依据，"
            "画面中的空间结构、建筑布局与光线氛围须与基准图保持一致"
        )
    rules = "\n".join(identity_rules)
    # Shot 模型没有 description 字段（原写法会抛 AttributeError，image_prompt 一为空就整条链路中断）。
    # 兜底改用 shot_purpose：它是镜头目的描述，比空串更有信息量；仍为空时留空，不虚构画面内容。
    body = shot.image_prompt or shot.shot_purpose or ""
    parts = [p for p in (head, rules, f"画面内容：{body}" if body else "") if p]
    return "\n".join(parts)


def _clip_prompt(shot: Shot, kf_key: str, char_key: str | None, char_name: str | None,
                 scene_key: str, scene_title: str, prev_key: str | None) -> str:
    # 仅角色/场景用 @名称 书写（解析后注入 ref_identity / ref_scene）。
    # ref_image（本镜分镜图）与 ref_prev_frame（上一镜末帧）属系统保留槽位，
    # 由执行器按镜头实际产物回填；提示词中不写 @ 引用，避免占用保留槽位。
    refs = [f"参考场景 @{scene_title}"] if scene_title else []
    if char_name:
        refs.append(f"参考角色 @{char_name}")
    head = "；".join(refs)
    body = shot.video_prompt or shot.image_prompt or ""
    return f"{head}。动作与运镜：{body}" if body else head


def _compiled_asset_prompt(db: Session, asset_type: str, entity_id: str) -> str | None:
    """优先使用统一硬化模板（compile_asset_image）生成资产提示词。

    历史问题：批量出图曾用本模块内的极简拼接（角色仅『定妆图，正面全身，纯色背景』，
    82-172 字），而重新生成走 prompt_compiler 的硬化模板（568-893 字，含纯净度分层、
    视图规范、一致性约束），两条链路提示词不一致——模板硬化只对重新生成生效，
    批量出的资产完全没有约束，这是『资产图不合要求』的直接原因。
    现统一收敛到同一模板；编译异常或产物为空时回退极简版，保证批量出图不中断。
    """
    try:
        from app.services.prompt_compiler import compile_asset_image

        compiled = compile_asset_image(db, asset_type, entity_id)
        prompt = (compiled.positive_prompt or "").strip()
        if prompt:
            return prompt
        logger.warning("资产提示词编译为空 asset_type=%s entity=%s，回退极简版", asset_type, entity_id)
    except Exception as _ce:  # noqa: BLE001 - 编译失败不阻断批量出图
        logger.warning("资产提示词编译失败 asset_type=%s entity=%s: %s", asset_type, entity_id, _ce)
    return None


def _upsert_plan(db: Session, project, plan_type: str, asset_key: str, prompt: str,
                 owner_type: str, owner_id: str, name: str,
                 episode: int | None = None, seq: int | None = None) -> AssetPlan:
    plan = (
        db.query(AssetPlan)
        .filter(AssetPlan.project_id == project.id, AssetPlan.asset_key == asset_key)
        .first()
    )
    path = make_media_path(project.id, asset_key, plan_type)
    if plan is None:
        plan = AssetPlan(
            id=ids.asset_plan_id(), project_id=project.id, plan_type=plan_type,
            asset_key=asset_key, media_path=path, prompt=prompt, status="pending",
            owner_type=owner_type, owner_id=owner_id, episode=episode, seq=seq, version=1,
        )
        db.add(plan)
        db.flush()
    else:
        # 名称与路径固定不可变：key 与 path 绝不改动；
        # 提示词随最新解析结果刷新（内容可变，名称/路径不变）
        if prompt and plan.prompt != prompt:
            plan.prompt = prompt
    return plan


def _bind_dep(db: Session, plan: AssetPlan, slot: str, ref_key: str, ref_plan: AssetPlan | None) -> None:
    dep = (
        db.query(AssetPlanDependency)
        .filter(AssetPlanDependency.plan_id == plan.id, AssetPlanDependency.slot == slot)
        .first()
    )
    if dep is None:
        dep = AssetPlanDependency(
            id=ids.asset_plan_dep_id(), plan_id=plan.id, slot=slot,
            ref_key=ref_key, ref_plan_id=ref_plan.id if ref_plan else None,
            ref_path=ref_plan.media_path if ref_plan else None,
        )
        db.add(dep)
        db.flush()
    else:
        dep.ref_key = ref_key
        dep.ref_plan_id = ref_plan.id if ref_plan else None
        dep.ref_path = ref_plan.media_path if ref_plan else None


_KEYFRAME_ALLOWED_SLOTS = ("ref_identity", "ref_character_2", "ref_scene", "ref_prop", "ref_character_3", "ref_character_4", "ref_character_5", "ref_character_6")
_CLIP_ALLOWED_SLOTS = ("ref_image", "ref_identity", "ref_character_2", "ref_prop", "ref_prev_frame", "ref_character_3", "ref_character_4", "ref_character_5", "ref_character_6")


def _prune_deps(db: Session, plan: AssetPlan, allowed: tuple) -> int:
    """清理不再有效的槽位依赖（如早期错误绑定的 ref_prev_frame），返回删除条数。"""
    stale = (
        db.query(AssetPlanDependency)
        .filter(AssetPlanDependency.plan_id == plan.id)
        .all()
    )
    removed = 0
    for dep in stale:
        if dep.slot not in allowed:
            db.delete(dep)
            removed += 1
    if removed:
        db.flush()
    return removed


def sync_plan_after_generate(db: Session, job, created_assets: list) -> int:
    # 生成成功后回写定义：绑定正式资产并置为已生成，名称与路径保持不变
    if not created_assets:
        return 0
    plans = (
        db.query(AssetPlan)
        .filter(AssetPlan.project_id == job.project_id, AssetPlan.status == "pending")
        .all()
    )
    if not plans:
        return 0
    by_owner: dict = {}
    for p in plans:
        if p.owner_id:
            group = p.plan_type if p.plan_type in ("character", "scene", "prop") else "shot"
            by_owner[(group, p.owner_id, p.plan_type)] = p
    shot_id = job.shot_id
    params = job.params or {}
    targets = []
    for a in created_assets:
        atype = a.get("asset_type") or "keyframe"
        if atype in ("character", "scene", "prop"):
            owner_key = {"character": "__character_id__", "scene": "__scene_id__", "prop": "__prop_id__"}[atype]
            oid = params.get(owner_key)
            if oid:
                plan = by_owner.get((atype, oid, atype))
                if plan is not None:
                    targets.append((plan, a["asset_id"]))
        elif atype in ("keyframe", "clip") and shot_id:
            plan = by_owner.get(("shot", shot_id, atype))
            if plan is not None:
                targets.append((plan, a["asset_id"]))
    for plan, asset_id in targets:
        plan.asset_id = asset_id
        plan.status = "generated"
    db.flush()
    return len(targets)


def build_asset_plans(db: Session, project, episode_start: int = 1,
                      episode_count: int = 1) -> dict:
    """为项目内全部实体定稿资产定义与依赖；幂等，可重复调用不改变既有名称与路径。"""
    stats = {"character": 0, "scene": 0, "prop": 0, "keyframe": 0, "clip": 0, "deps": 0}
    chars = db.query(Character).filter(Character.project_id == project.id).all()
    props = db.query(Prop).filter(Prop.project_id == project.id).all()
    scenes = db.query(Scene).filter(Scene.project_id == project.id).all()

    char_plans: dict[str, AssetPlan] = {}
    for c in chars:
        key = make_asset_key("character", c.name, f"{project.id}|{c.id}")
        _ch_prompt = _compiled_asset_prompt(db, "character", c.id) or _character_prompt(c)
        char_plans[c.id] = _upsert_plan(db, project, "character", key, _ch_prompt,
                                        "character", c.id, c.name)
        stats["character"] += 1

    prop_plans: dict[str, AssetPlan] = {}
    for p in props:
        key = make_asset_key("prop", p.name, f"{project.id}|{p.id}")
        _pp_prompt = _compiled_asset_prompt(db, "prop", p.id) or _prop_prompt(p)
        prop_plans[p.id] = _upsert_plan(db, project, "prop", key, _pp_prompt,
                                        "prop", p.id, p.name)
        stats["prop"] += 1

    # 道具名 → 定义映射，供分镜图按提示词中的 @道具名 绑定 ref_prop
    prop_by_name: dict[str, AssetPlan] = {
        p.name: prop_plans[p.id] for p in props if p.id in prop_plans and p.name
    }
    scene_plans: dict[str, AssetPlan] = {}
    char_names = [c.name for c in chars if c.name]
    # 场景板编译（P1）：此前 compile_scene_plate 全仓无调用方，空镜规格校验全部空转。
    # 放在 _scene_prompt 之前，使后者能读到 plate_text；
    # persist_fallback=False 保证无 LLM 配置时不把粗清洗结果固化成正式场景板
    # （否则 plate_text 非空即缓存命中，日后配好 LLM 也不会重新编译）。
    from app.services.scene_plate import compile_scene_plate
    _plate_stats = {"cached": 0, "llm": 0, "rules": 0, "rules_transient": 0, "error": 0}
    for s in scenes:
        try:
            _r = compile_scene_plate(db, s, persist_fallback=False)
            _plate_stats[_r.get("source") or "rules"] = (
                _plate_stats.get(_r.get("source") or "rules", 0) + 1
            )
        except Exception as _pe:  # noqa: BLE001 - 场景板失败不阻断资产定义，仅记录
            _plate_stats["error"] += 1
            logger.warning("场景板编译失败 scene=%s: %s", s.id, _pe)
    stats["scene_plate"] = _plate_stats
    for s in scenes:
        key = make_asset_key("scene", s.title, f"{project.id}|{s.id}")
        _sc_prompt = _compiled_asset_prompt(db, "scene", s.id) or _scene_prompt(s, char_names)
        scene_plans[s.id] = _upsert_plan(db, project, "scene", key, _sc_prompt,
                                         "scene", s.id, s.title)
        stats["scene"] += 1

    ep_end = episode_start + episode_count - 1
    shots = (
        db.query(Shot).join(Scene, Scene.id == Shot.scene_id)
        .filter(Scene.project_id == project.id, Shot.episode >= episode_start, Shot.episode <= ep_end)
        .order_by(Shot.episode, Shot.seq).all()
    )

    char_by_id = {c.id: c for c in chars}
    scene_by_id = {s.id: s for s in scenes}
    kf_plans: dict[str, AssetPlan] = {}
    prev_kf_key: dict[int, str] = {}

    for shot in shots:
        scene = scene_by_id.get(shot.scene_id)
        if scene is None:
            continue
        scene_plan = scene_plans.get(scene.id)
        char = char_by_id.get(shot.character_id) if shot.character_id else None
        char_plan = char_plans.get(char.id) if char else None
        idx = shot.seq
        kf_key = make_asset_key("keyframe", char.name if char else scene.title,
                                f"{project.id}|{shot.id}", seq=idx)
        kf_prompt = _keyframe_prompt(shot, char_plan.asset_key if char_plan else None,
                                     char.name if char else None,
                                     scene_plan.asset_key if scene_plan else "", scene.title)
        kf_plan = _upsert_plan(db, project, "keyframe", kf_key, kf_prompt, "shot", shot.id,
                               char.name if char else scene.title, episode=shot.episode, seq=idx)
        kf_plans[shot.id] = kf_plan
        stats["keyframe"] += 1

        if char_plan:
            # 单角色只占 ref_identity 一个槽位：此前同一角色同时绑定 ref_identity 与
            # ref_character_2，注入时同一路径只占一个槽位（_inject_dependency_slots
            # 按 used_paths 去重），ref_character_2 实际从未生效，徒增误导性绑定记录
            _bind_dep(db, kf_plan, "ref_identity", char_plan.asset_key, char_plan)
            stats["deps"] += 1
        if scene_plan:
            _bind_dep(db, kf_plan, "ref_scene", scene_plan.asset_key, scene_plan)
            stats["deps"] += 1
        # 道具参考：仅当镜头提示词显式 @道具名 时绑定（ref_prop 单槽位，取首个匹配）
        if prop_by_name and shot.image_prompt:
            from app.services.ref_inject import extract_refs
            for _ref_name in extract_refs(shot.image_prompt):
                _prop_plan = prop_by_name.get(_ref_name)
                if _prop_plan is not None:
                    _bind_dep(db, kf_plan, "ref_prop", _prop_plan.asset_key, _prop_plan)
                    stats["deps"] += 1
                    break
        _prune_deps(db, kf_plan, _KEYFRAME_ALLOWED_SLOTS)

        clip_key = make_asset_key("clip", char.name if char else scene.title,
                                  f"{project.id}|{shot.id}", seq=idx)
        clip_prompt = _clip_prompt(shot, kf_plan.asset_key, char_plan.asset_key if char_plan else None,
                                   char.name if char else None,
                                   scene_plan.asset_key if scene_plan else "", scene.title,
                                   prev_kf_key.get(shot.episode))
        clip_plan = _upsert_plan(db, project, "clip", clip_key, clip_prompt, "shot", shot.id,
                                 char.name if char else scene.title, episode=shot.episode, seq=idx)
        stats["clip"] += 1
        _bind_dep(db, clip_plan, "ref_image", kf_plan.asset_key, kf_plan)
        stats["deps"] += 1
        if char_plan:
            _bind_dep(db, clip_plan, "ref_identity", char_plan.asset_key, char_plan)
            stats["deps"] += 1

        # ref_prev_frame 指向的是上一镜“末帧”而非上一镜分镜图，
        # 由执行器按 shot.last_frame_asset_id 回填，此处不做错误绑定。
        _prune_deps(db, clip_plan, _CLIP_ALLOWED_SLOTS)
        prev_kf_key[shot.episode] = kf_plan.asset_key

    # 清理孤儿定义：镜头被新解析结果替换后，其定义与依赖不再有效
    live_shot_ids = {s.id for s in shots}
    orphan = (
        db.query(AssetPlan)
        .filter(AssetPlan.project_id == project.id,
                AssetPlan.owner_type == "shot",
                AssetPlan.asset_id.is_(None))
        .all()
    )
    for plan in orphan:
        if plan.owner_id and plan.owner_id not in live_shot_ids:
            db.query(AssetPlanDependency).filter(
                AssetPlanDependency.plan_id == plan.id
            ).delete(synchronize_session=False)
            db.delete(plan)

    db.flush()
    return stats
