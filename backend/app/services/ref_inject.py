"""@引用注入服务（D-16）：解析提示词中的 @角色名 / @场景名，
自动映射到对应定妆图 / 基准图文件路径，作为参考图注入工作流媒体参数。
设计约定：
- 引用语法：@名称（与剧本解析的 @名称 引用规则一致，角色改名时同步改写）
- 映射规则：@角色名 → 角色 portrait_asset_id 对应文件；@场景名 → 场景 baseline_asset_id 对应文件；
  @道具名 → 道具 anchor_asset_id 对应文件
- 多角色支持：多个 @角色 按出现顺序依次填入工作流声明的角色槽位
  （ref_identity → ref_character_2 → ref_character_3…），槽位不足时返回 422 提示引用过多，
  不静默丢参考
- 工作流未声明某类槽位时（典型：H3 Ref2VA 没有 ref_scene），该类 @引用降级为文字描述，
  不注入、不报错；是否降级由 capabilities 声明决定，绝不按工作流名称推断
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

# 角色槽位优先级：第 1 个 @角色 → ref_identity（身份参考），第 2 个 → ref_character_2（角色参考），
# 更多角色按 ref_character_N 依次填充（7 槽多参考图工作流）。
# P0 整改：ref_image 为当前镜头分镜图专用槽位（H3 ref2va 的 ref_image_1、Qwen Edit 的 image1），
# 此前被多角色引用占用，导致分镜图被角色定妆图覆盖而丢失；角色引用不再占用该槽位
_CHARACTER_SLOTS = ["ref_identity", "ref_character_2"]
# 场景槽位：仅 ref_scene 一个
_SCENE_SLOTS = ["ref_scene"]
# 道具槽位：ref_prop / ref_prop_2（多参考图工作流）
_PROP_SLOTS = ["ref_prop"]

# 系统保留槽位：由生成流程按镜头/连续性规则写入，@引用不得占用
RESERVED_SLOTS = frozenset({"ref_image", "ref_prev_frame", "first_frame", "end_frame"})


def is_character_slot(name: str) -> bool:
    """按槽位名判断是否角色参考槽（工作流可声明任意多个）。

    ref_character_2 是第二个角色身份槽位，与 ref_identity 同类。
    """
    if name in RESERVED_SLOTS:
        return False
    return name.startswith(("ref_identity", "ref_character", "ref_char"))


def is_scene_slot(name: str) -> bool:
    return name.startswith("ref_scene")


def is_prop_slot(name: str) -> bool:
    return name.startswith("ref_prop")


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
    多角色：按出现顺序依次填入可用角色槽位（ref_identity → ref_image → ref_character_2），
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
        # 未提供能力声明时一律视为「不声明任何槽位」，不回退到内置默认槽位表：
        # 默认表把 ref_scene 当成所有工作流通用槽位，会给 H3 Ref2VA 注入它并不存在的
        # 输入参数（ref_scene 只是 Qwen 图片工作流的合法参数）。
        avail_char = []
        avail_scene = []
        avail_prop = []
    else:
        slot_names = [
            item.get("param") if isinstance(item, dict) else item
            for item in reference_slots
        ]
        # 按工作流声明顺序分类，槽位数量由工作流决定（不再写死 2 个角色槽）
        avail_char = [s for s in slot_names if is_character_slot(s)]
        avail_scene = [s for s in slot_names if is_scene_slot(s)]
        avail_prop = [s for s in slot_names if is_prop_slot(s)]
    media: dict[str, str] = {}
    # 角色引用：按顺序填入可用角色槽位。
    # 工作流完全没有角色槽位时降级为纯文字描述（与提示词编译器的
    # character_reference_unsupported 告警一致）；只有『声明了槽位却装不下』才 422。
    if char_names and avail_char and len(char_names) > len(avail_char):
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
    # 场景引用：按顺序填入可用场景槽位。
    # H3 Ref2VA 没有 ref_scene 槽位：此时不注入场景图，场景仅由提示词文字描述承担，
    # 绝不把 ref_scene 写进它不支持的输入参数里。
    if scene_names and avail_scene and len(scene_names) > len(avail_scene):
        raise errors.AppError(
            "invalid_reference",
            f"提示词引用了 {len(scene_names)} 个场景（{'、'.join(scene_names)}），"
            f"但当前工作流仅支持 {len(avail_scene)} 个场景参考槽位（{avail_scene}），请减少场景引用",
            422,
        )
    for name, slot in zip(scene_names, avail_scene):
        media[slot] = refs[name]["file_path"]
    # 道具引用：按顺序填入可用道具槽位（工作流未声明道具槽位时同样降级为文字描述）
    if prop_names and avail_prop and len(prop_names) > len(avail_prop):
        raise errors.AppError(
            "invalid_reference",
            f"提示词引用了 {len(prop_names)} 个道具（{'、'.join(prop_names)}），"
            f"但当前工作流仅支持 {len(avail_prop)} 个道具参考槽位（{avail_prop}），请减少道具引用",
            422,
        )
    for name, slot in zip(prop_names, avail_prop):
        media[slot] = refs[name]["file_path"]
    return media


# ---------- 图片引用编号重写（P0 错位修复） ----------
# 提示词中的图片引用编号由 LLM 按文本顺序书写，与实际注入槽位顺序无关；
# 生成前必须按『@名称 → 资产 → 实际槽位 → input_key 序号』重写，
# 保证提示词描述与模型实际收到的图片严格一致。
_IMAGE_KEY_RE = re.compile(r"^images\.image_(\d+)$")
_REFIMG_KEY_RE = re.compile(r"^ref_images\.ref_image_(\d+)$")
_REF_TAG_RE = re.compile(r"<(?:Picture|picture|image|Image)\s*(\d+)>")


def slot_image_index(slot: str, reference_slots: list | None) -> int | None:
    """返回槽位在工作流中的图片序号（1-based）。
    - images.image_N（Qwen-Image-2.1 TextEncodeQwenImage21）→ N
    - ref_images.ref_image_N（H3 ref2va autogrow，0-based）→ N+1
    """
    for item in reference_slots or []:
        if not isinstance(item, dict) or item.get("param") != slot:
            continue
        key = str(item.get("input_key") or "")
        m = _IMAGE_KEY_RE.match(key)
        if m:
            return int(m.group(1))
        m = _REFIMG_KEY_RE.match(key)
        if m:
            return int(m.group(1)) + 1
    return None


def workflow_ref_syntax(reference_slots: list | None) -> str | None:
    """判断工作流提示词图片引用语法：image（<imageN>）或 picture（<Picture N>）。"""
    for item in reference_slots or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("input_key") or "")
        if key.startswith("images.image_"):
            return "image"
        if key.startswith("ref_images.ref_image_"):
            return "picture"
    return None


def rewrite_picture_refs(db: Session, project_id: str, prompt: str,
                         media: dict[str, str] | None,
                         reference_slots: list | None) -> str:
    """按实际注入槽位重写提示词中的图片引用编号并统一为目标工作流语法。

    - media 为实际注入的 {槽位: 文件路径}（build_media_params 或 _inject_dependency_slots 产物）
    - 逐段（按；。切分）处理：段内恰有可解析 @名称 且含图片引用时，
      按该名称实际槽位的 input_key 序号改号
    - @名称解析失败或未注入时保持原样，不阻断生成（由既有门禁负责报错）
    - 幂等：重复调用结果一致
    """
    if not prompt or not media:
        return prompt
    syntax = workflow_ref_syntax(reference_slots)
    if syntax is None:
        return prompt
    path_to_slot = {path: slot for slot, path in media.items()}
    try:
        refs = resolve_refs(db, project_id, prompt)
    except Exception:  # noqa: BLE001 - 引用解析失败保持原样，不阻断生成
        return prompt
    name_to_index: dict[str, int] = {}
    for name, info in refs.items():
        slot = path_to_slot.get(info.get("file_path"))
        if slot is None:
            continue
        idx = slot_image_index(slot, reference_slots)
        if idx is not None:
            name_to_index[name] = idx
    if not name_to_index:
        return prompt
    out_lines: list[str] = []
    for line in prompt.split("\n"):
        if not _REF_TAG_RE.search(line):
            out_lines.append(line)
            continue
        rebuilt: list[str] = []
        for seg in re.split(r"([；;。])", line):
            names = [n for n in extract_refs(seg) if n in name_to_index]
            if names and _REF_TAG_RE.search(seg):
                idx = name_to_index[names[0]]
                tag = f"<image{idx}>" if syntax == "image" else f"<Picture {idx}>"
                seg = _REF_TAG_RE.sub(tag, seg)
            rebuilt.append(seg)
        out_lines.append("".join(rebuilt))
    # 图序绑定：按实际注入槽位重写『参考图职责』，让文字里的「图N」与模型收到的第N张严格一致
    from app.services.prompt_qc import relabel_reference_block
    return relabel_reference_block(
        db, project_id, "\n".join(out_lines), media, reference_slots)
