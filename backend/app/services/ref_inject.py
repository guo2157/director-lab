"""@引用注入服务（D-16）：解析提示词中的 @角色名 / @场景名，
自动映射到对应定妆图 / 基准图文件路径，作为参考图注入工作流媒体参数。
设计约定：
- 引用语法：@名称（与剧本解析的 @名称 引用规则一致，角色改名时同步改写）
- 映射规则：@角色名 → 角色 portrait_asset_id 对应文件；@场景名 → 场景 baseline_asset_id 对应文件；
  @道具名 → 道具 anchor_asset_id 对应文件
- 多角色支持：多个 @角色 按出现顺序依次填入角色槽位（优先级 ref_identity → ref_image → ref_costume），
  槽位不足时返回 422 提示引用过多，不静默丢参考
- 同名兜底：角色与场景同名时（仅可能来自历史数据，创建/改名入口已拦截）返回 422
- 未匹配的引用明确报错（invalid_reference），不静默忽略
- 角色无定妆图 / 场景无基准图时报错（reference_asset_missing），提示先生成
- 仅注入工作流声明的槽位：未声明的槽位不注入（由工作流 capabilities 决定）
"""
import re
from sqlalchemy.orm import Session
from app.models import Asset, Character, Prop, Scene
from app.services import errors

# @引用语法：@后跟非空白字符（中文/字母/数字/下划线/间隔号）
# 间隔号『·』用于场景标题等实体命名（如『花园·红花丛前的草地』），
# 不纳入会导致引用名被截断为前半段，触发 invalid_reference（2026-10-28 全流程测试实证）
_REF_RE = re.compile(r"@([\w\u4e00-\u9fff·]+)")

# 角色槽位优先级：第 1 个 @角色 → ref_identity（身份参考），第 2 个 → ref_costume（服装参考）
# P0 整改：ref_image 为当前镜头分镜图专用槽位（H3 ref2va 的 ref_image_1、Qwen Edit 的 image1），
# 此前被多角色引用占用，导致分镜图被角色定妆图覆盖而丢失；角色引用不再占用该槽位
_CHARACTER_SLOTS = ["ref_identity", "ref_costume"]
# 场景槽位：仅 ref_scene 一个
_SCENE_SLOTS = ["ref_scene"]
# 道具槽位：仅 ref_prop 一个（ref2va 工作流）
_PROP_SLOTS = ["ref_prop"]

# 系统保留槽位：由生成流程按镜头/连续性规则写入，@引用不得占用
RESERVED_SLOTS = frozenset({"ref_image", "ref_prev_frame", "first_frame", "end_frame"})


def extract_refs(text: str) -> list[str]:
    """提取提示词中的全部 @引用名称（去重保序）。"""
    seen: list[str] = []
    for m in _REF_RE.finditer(text or ""):
        name = m.group(1)
        if name not in seen:
            seen.append(name)
    return seen


def resolve_refs(db: Session, project_id: str, prompt: str) -> dict[str, dict]:
    """解析提示词中的 @引用，返回 {名称: {type, asset_id, file_path}}。
    未匹配或资产缺失时抛 AppError，不静默忽略。
    """
    refs: dict[str, dict] = {}
    for name in extract_refs(prompt):
        ch = db.query(Character).filter(
            Character.project_id == project_id, Character.name == name
        ).first()
        sc = db.query(Scene).filter(
            Scene.project_id == project_id, Scene.title == name
        ).first()
        pr = db.query(Prop).filter(
            Prop.project_id == project_id, Prop.name == name
        ).first()
        # 同名兜底（历史数据）：创建/改名入口已拦截同名，此处仅防御存量数据
        matched = [t for t, o in (("角色", ch), ("场景", sc), ("道具", pr)) if o is not None]
        if len(matched) > 1:
            raise errors.AppError(
                "invalid_reference",
                f"『{name}』同时是{'与'.join(matched)}的名称，无法确定引用对象，请修改名称后重试",
                422,
            )
        if ch is not None:
            if not ch.portrait_asset_id:
                raise errors.AppError(
                    "reference_asset_missing",
                    f"角色『{name}』尚未生成定妆图，请先生成定妆图再引用",
                    409,
                )
            asset = db.get(Asset, ch.portrait_asset_id)
            if asset is None:
                raise errors.AppError(
                    "reference_asset_missing", f"角色『{name}』定妆图资产不存在", 409)
            refs[name] = {"type": "character", "asset_id": asset.id, "file_path": asset.file_path}
            continue
        if sc is not None:
            if not sc.baseline_asset_id:
                raise errors.AppError(
                    "reference_asset_missing",
                    f"场景『{name}』尚未生成基准图，请先生成基准图再引用",
                    409,
                )
            asset = db.get(Asset, sc.baseline_asset_id)
            if asset is None:
                raise errors.AppError(
                    "reference_asset_missing", f"场景『{name}』基准图资产不存在", 409)
            refs[name] = {"type": "scene", "asset_id": asset.id, "file_path": asset.file_path}
            continue
        if pr is not None:
            if not pr.anchor_asset_id:
                raise errors.AppError(
                    "reference_asset_missing",
                    f"道具『{name}』尚未生成锚定图，请先生成锚定图再引用",
                    409,
                )
            asset = db.get(Asset, pr.anchor_asset_id)
            if asset is None:
                raise errors.AppError(
                    "reference_asset_missing", f"道具『{name}』锚定图资产不存在", 409)
            refs[name] = {"type": "prop", "asset_id": asset.id, "file_path": asset.file_path}
            continue
        raise errors.AppError(
            "invalid_reference",
            f"提示词中的 @『{name}』未匹配到项目内角色、场景或道具，请修正引用名称",
            422,
        )
    return refs


def build_media_params(db: Session, project_id: str, prompt: str,
                        reference_slots: list[str] | None) -> dict[str, str]:
    """将 @引用解析为工作流媒体参数 {param_name: local_file_path}。
    reference_slots 为工作流 capabilities.reference_slots；
    仅注入工作流声明的槽位，未声明的槽位不注入。
    多角色：按出现顺序依次填入可用角色槽位（ref_identity → ref_image → ref_costume），
    槽位不足时返回 422，不静默丢参考。
    """
    refs = resolve_refs(db, project_id, prompt)
    # 按引用类型分组，保持提示词中的出现顺序
    char_names = [n for n, i in refs.items() if i["type"] == "character"]
    scene_names = [n for n, i in refs.items() if i["type"] == "scene"]
    prop_names = [n for n, i in refs.items() if i["type"] == "prop"]
    # 工作流声明的可用槽位（None 表示不限制，全部可用）
    # 兼容两种声明格式：字符串数组 ['ref_identity', ...] 或对象数组
    # [{param: 'ref_identity', input_key: 'ref_images.ref_image_2', optional: true}, ...]（seed meta 格式）
    if reference_slots is None:
        avail_char = list(_CHARACTER_SLOTS)
        avail_scene = list(_SCENE_SLOTS)
        avail_prop = list(_PROP_SLOTS)
    else:
        slot_names = [
            item.get("param") if isinstance(item, dict) else item
            for item in reference_slots
        ]
        avail_char = [s for s in _CHARACTER_SLOTS if s in slot_names]
        avail_scene = [s for s in _SCENE_SLOTS if s in slot_names]
        avail_prop = [s for s in _PROP_SLOTS if s in slot_names]
    media: dict[str, str] = {}
    # 角色引用：按顺序填入可用角色槽位
    if len(char_names) > len(avail_char):
        raise errors.AppError(
            "invalid_reference",
            f"提示词引用了 {len(char_names)} 个角色（{'、'.join(char_names)}），"
            f"但当前工作流仅支持 {len(avail_char)} 个角色参考槽位（{avail_char}），请减少角色引用或更换工作流",
            422,
        )
    for name, slot in zip(char_names, avail_char):
        if slot in RESERVED_SLOTS:
            # 防御：即便工作流声明把系统槽位当角色槽位，也不覆盖分镜图/首尾帧
            continue
        media[slot] = refs[name]["file_path"]
    # 场景引用：按顺序填入可用场景槽位
    if len(scene_names) > len(avail_scene):
        raise errors.AppError(
            "invalid_reference",
            f"提示词引用了 {len(scene_names)} 个场景（{'、'.join(scene_names)}），"
            f"但当前工作流仅支持 {len(avail_scene)} 个场景参考槽位（{avail_scene}），请减少场景引用",
            422,
        )
    for name, slot in zip(scene_names, avail_scene):
        media[slot] = refs[name]["file_path"]
    # 道具引用：按顺序填入可用道具槽位
    if len(prop_names) > len(avail_prop):
        raise errors.AppError(
            "invalid_reference",
            f"提示词引用了 {len(prop_names)} 个道具（{'、'.join(prop_names)}），"
            f"但当前工作流仅支持 {len(avail_prop)} 个道具参考槽位（{avail_prop}），请减少道具引用",
            422,
        )
    for name, slot in zip(prop_names, avail_prop):
        media[slot] = refs[name]["file_path"]
    return media
