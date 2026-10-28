"""资产定义生成：解析落库后一次性定稿名称、占位路径、提示词与参考依赖。

约定（本模块为命名的唯一权威来源）：
- 名称固定：{类型前缀}_{语义片段}_{8 位短哈希}，一经生成永不修改；
  分镜图/分镜视频额外带 集/场景/镜头 序号，便于人工核对。
- 路径固定：data/media/plans/{project_id}/{asset_key}.png|mp4，生成前即确定。
- 提示词：公共资产写实描述；分镜图/分镜视频按模板引用已定稿的参考资产名。
- 状态：pending 待生成，成功产出后由生成环节回写 generated 与 asset_id。
"""
import hashlib
import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import ids
from app.models.asset_plan import AssetPlan, AssetPlanDependency
from app.models.narrative import Character, Prop, Scene, Shot

PLAN_PREFIX = {"character": "chr", "scene": "scn", "prop": "prp", "keyframe": "kf", "clip": "clip"}
PLAN_EXT = {"character": ".png", "scene": ".png", "prop": ".png", "keyframe": ".png", "clip": ".mp4"}
# 分镜图槽位：角色身份 / 服装 / 场景 / 道具
KEYFRAME_SLOTS = ("ref_identity", "ref_costume", "ref_scene", "ref_prop")
# 分镜视频槽位：当前分镜图 / 角色 / 场景 / 上一镜末帧
CLIP_SLOTS = ("ref_image", "ref_identity", "ref_scene", "ref_prev_frame")


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


def _scene_prompt(s: Scene) -> str:
    parts = [s.title]
    if s.description:
        parts.append(s.description)
    if s.time_note:
        parts.append(s.time_note)
    parts.append("场景基准图，无主要人物，广角全景，高清写实，保持空间结构与光线稳定")
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
    # 槽位顺序由引用类型分组决定（角色 ref_identity→ref_costume，场景 ref_scene，
    # 道具 ref_prop），与文本先后无关，此处先场景后角色仅为可读性。
    refs = [f"参考场景 @{scene_title}"] if scene_title else []
    if char_name:
        refs.append(f"参考角色 @{char_name}")
    head = "；".join(refs)
    body = shot.image_prompt or shot.description or ""
    return f"{head}。画面内容：{body}" if body else head


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


_KEYFRAME_ALLOWED_SLOTS = ("ref_identity", "ref_costume", "ref_scene", "ref_prop")
_CLIP_ALLOWED_SLOTS = ("ref_image", "ref_identity", "ref_scene")


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
        char_plans[c.id] = _upsert_plan(db, project, "character", key, _character_prompt(c),
                                        "character", c.id, c.name)
        stats["character"] += 1

    prop_plans: dict[str, AssetPlan] = {}
    for p in props:
        key = make_asset_key("prop", p.name, f"{project.id}|{p.id}")
        prop_plans[p.id] = _upsert_plan(db, project, "prop", key, _prop_prompt(p),
                                        "prop", p.id, p.name)
        stats["prop"] += 1

    # 道具名 → 定义映射，供分镜图按提示词中的 @道具名 绑定 ref_prop
    prop_by_name: dict[str, AssetPlan] = {
        p.name: prop_plans[p.id] for p in props if p.id in prop_plans and p.name
    }
    scene_plans: dict[str, AssetPlan] = {}
    for s in scenes:
        key = make_asset_key("scene", s.title, f"{project.id}|{s.id}")
        scene_plans[s.id] = _upsert_plan(db, project, "scene", key, _scene_prompt(s),
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
            _bind_dep(db, kf_plan, "ref_identity", char_plan.asset_key, char_plan)
            _bind_dep(db, kf_plan, "ref_costume", char_plan.asset_key, char_plan)
            stats["deps"] += 2
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
        if scene_plan:
            _bind_dep(db, clip_plan, "ref_scene", scene_plan.asset_key, scene_plan)
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
