"""镜头资产清单与参考绑定（P2）：分镜图生成时按镜头实际内容自动绑定参考资产。

设计约定（对应分镜一致性专项改造第六/七节）：
- 镜头资产清单来源：出场角色（character_id + dialogue_segments 说话人）+ 所属场景（scene_id）
  + 提示词中已显式 @引用 的道具
- 分镜图生成不能只依赖提示词中恰好写了 @名称：清单内未在提示词出现的角色/场景，
  以『参考资产』段自动补入提示词头部，使 @引用解析（ref_inject）能携带参考图
- 不相关的资产不传入：只注入镜头实际出场实体，不把项目全部资产灌入
- 道具半自动注入：提示词正文明确提到道具名称且该道具已有定妆图资产时，
  自动补入 @引用以携带道具参考图；提示词未提到的道具不注入，避免无关道具混入分镜
"""
from sqlalchemy.orm import Session
from app.models import Character, Prop, Scene, Shot


def collect_shot_characters(db: Session, shot: Shot) -> list[str]:
    """收集镜头实际出场角色名（去重保序）：主说话角色 + 对白分段说话人。"""
    names: list[str] = []
    if shot.character_id:
        ch = db.get(Character, shot.character_id)
        if ch is not None and ch.name:
            names.append(ch.name)
    for seg in shot.dialogue_segments or []:
        if not isinstance(seg, dict):
            continue
        spk = seg.get("speaker") or seg.get("speaker_id")
        if spk and spk not in names:
            names.append(spk)
    return names


def build_shot_asset_manifest(db: Session, shot: Shot) -> dict:
    """生成镜头资产清单：出场角色名列表 + 场景标题 + 道具名（显式 @引用 或提示词正文提及）。

    返回 {"characters": [名...], "scene": 标题或 None, "props": [名...]}，
    供任务快照与前端展示使用（镜头-资产可追踪引用关系）。
    """
    from app.services.ref_inject import extract_refs

    scene = db.get(Scene, shot.scene_id)
    prompt_text = shot.image_prompt or ""
    prop_names: list[str] = []
    if scene is not None and prompt_text:
        scene_title = scene.title or ""
        from app.models import Prop
        for ref in extract_refs(prompt_text):
            if ref == scene_title:
                continue
            prop = (
                db.query(Prop)
                .filter(Prop.project_id == scene.project_id, Prop.name == ref)
                .first()
            )
            if prop is not None:
                prop_names.append(ref)
        # 与 inject_asset_refs 口径对齐：提示词正文明确提到（未显式 @引用）的道具也纳入清单
        for prop in db.query(Prop).filter(Prop.project_id == scene.project_id).all():
            if (
                prop.name
                and prop.name not in prop_names
                and prop.name != scene_title
                and prop.name in prompt_text
            ):
                prop_names.append(prop.name)
    return {
        "characters": collect_shot_characters(db, shot),
        "scene": scene.title if scene is not None else None,
        "props": prop_names,
    }


def inject_asset_refs(db: Session, shot: Shot, prompt: str, *, with_qc: bool = False) -> str:
    """把镜头资产清单中未在提示词出现的角色/场景/道具以 @引用 补入提示词头部。

    - 已显式 @引用 的实体不重复注入
- 实体尚无正式资产图（定妆图/基准图缺失）时不注入该 @引用，
      避免生成时因 reference_asset_missing 直接失败；缺失记录在返回提示词的注释段之外，
      由调用方通过 build_shot_asset_manifest 对比得知
    - 返回增强后的提示词；无任何可注入项时原样返回
    """
    if not prompt:
        return prompt
    from app.services.ref_inject import extract_refs

    existing_refs = set(extract_refs(prompt))
    scene = db.get(Scene, shot.scene_id)
    inject_names: list[str] = []
    # 出场角色
    for name in collect_shot_characters(db, shot):
        if name in existing_refs:
            continue
        ch = (
            db.query(Character)
            .filter(Character.project_id == scene.project_id, Character.name == name)
            .first()
        )
        if ch is not None and ch.portrait_asset_id:
            inject_names.append(name)
    # 所属场景
    if scene is not None and scene.title and scene.title not in existing_refs and scene.baseline_asset_id:
        inject_names.append(scene.title)
    # 道具注入：优先由生命周期状态（prop_shot_state）驱动，状态缺失时降级启发式
    # - visible 且 reference_required → 注入 @引用 + 位置/交互/外观锁定约束
    #   （规格：道具可见必须传参考图 + 位置 + 交互 + 外观锁定）
    # - not_introduced/destroyed/consumed 且正文提及 → 写禁止生成约束（不传参考图）
    # - offscreen/occluded → 不注入参考图也不禁止（画外不等于消失、遮挡不等于毁坏）
    constraint_lines: list[str] = []
    negative_names: list[str] = []
    visible_props: list[str] = []  # 本镜可见道具（用于单/多道具约束）
    # 交互状态 → 通用位置描述（纯代码通用规则，不写死任何故事特定元素）
    _INTERACTION_DESC = {
        "held": "被角色握在手中",
        "carried": "由角色背负/携带在身上",
        "worn": "佩戴在角色身上",
    }
    if scene is not None:
        from app.models import PropShotState

        state_map = {
            st.prop_id: st
            for st in db.query(PropShotState)
            .filter(
                PropShotState.project_id == scene.project_id,
                PropShotState.shot_id == shot.id,
            )
            .all()
        }
        for prop in db.query(Prop).filter(Prop.project_id == scene.project_id).all():
            if not prop.name or not prop.anchor_asset_id:
                continue
            st = state_map.get(prop.id)
            # 默认快照（visibility=unknown / existence=not_introduced / interaction=unassigned）
            # 不代表剧情判断，按无状态处理，避免误判成「该道具不存在」而注入矛盾禁令
            if st is not None and st.visibility == "unknown" \
                    and st.existence_state == "not_introduced" \
                    and st.interaction_state == "unassigned" \
                    and not st.reference_required and not st.position \
                    and not st.appearance_constraint:
                st = None
            if st is not None:
                if st.existence_state in ("not_introduced", "destroyed", "consumed") \
                        and prop.name in prompt:
                    negative_names.append(prop.name)
                    continue
                if st.reference_required and prop.name not in existing_refs:
                    inject_names.append(prop.name)
                if st.visibility == "visible":
                    visible_props.append(prop.name)
                    if prop.name in existing_refs:
                        continue
                    # 位置：优先显式 position，其次按交互状态通用推导
                    desc = st.position or _INTERACTION_DESC.get(st.interaction_state)
                    seg = f"@{prop.name} 外观须与其参考图一致"
                    if desc:
                        seg += f"，{desc}"
                    if st.appearance_constraint:
                        seg += f"，{st.appearance_constraint}"
                    constraint_lines.append(seg)
            elif prop.name in prompt and prop.name not in existing_refs:
                # 降级路径：该道具尚无生命周期状态快照，维持正文提及启发式
                inject_names.append(prop.name)
        # 多道具独立身份约束（规格：每道具独立身份约束，禁用拼图）
        if len(visible_props) >= 2:
            constraint_lines.append(
                "多个参考道具各自独立呈现，保持各自形状与颜色特征，禁止拼合为组合体或互相融合"
            )
        # 单 visible 道具（规格：道具可见时禁止添加其他道具）
        # 注意：@引用 后必须加空格，否则紧随的汉字会被 @引用正则并入引用名
        # （如『@麦袋外』触发 invalid_reference，2026-11-06 实测）
        elif len(visible_props) == 1:
            constraint_lines.append(
                f"除@{visible_props[0]} 之外，画面中不得额外添加其他道具"
            )
    if not inject_names and not constraint_lines and not negative_names:
        return prompt
    parts: list[str] = []
    if inject_names:
        parts.append("参考资产：" + " ".join(f"@{n}" for n in inject_names))
    if constraint_lines:
        parts.append("道具状态约束：" + "；".join(constraint_lines) + "。")
    body = "\n".join(parts) + "\n" + prompt
    if negative_names:
        # 负向约束不带 @ 前缀，避免被 @引用解析误判为需要参考图
        body += "\n禁止：画面中出现" + "、".join(negative_names) + "（剧情依据：该道具此时不存在）。"
    if with_qc:
        # 出图前质检：道具锚定图缺失直接阻断，多角色站位交由 LLM 复核修正
        from app.services.prompt_qc import apply_keyframe_qc
        body = apply_keyframe_qc(db, shot, body)
    return body
