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
    """收集镜头实际出场角色名（去重保序）。

    **委托给 ShotCast 单一出场清单（Phase 2）**：有 shot_plan 契约时以契约为准
    （画外声音不注入 @引用、不占身份槽位），无契约走旧启发式兜底
    （shot_subject_characters，行为不变）。

    2026-11-21 修复：本函数原本是独立的第二实现，只认
    ``character_id`` + ``dialogue_segments`` 说话人，与权威函数
    ``shot_subject_characters`` 的两份定义逐渐分叉。加了「结构化动作字段」
    这个来源之后，分叉直接变成了用户可见的 bug：

        提示词头部注入的是「参考资产：@江屿 @海岸沙滩·夕阳」——缺林晚

    因为 ``inject_asset_refs`` 走的是本函数，而资产定义（槽位绑定）走的是
    ``shot_subject_characters``。于是出现「提示词里两个角色、参考图只有
    一个槽位」的矛盾现象：槽位算对了，提示词没跟上，模型只能拿一张定妆图
    去画两个人，画面里就出现两个江屿。

    保留本函数名是因为 5 处调用方（regen_workspace / regeneration /
    build_shot_asset_manifest / inject_asset_refs）都用它，改名会扩大改动面。
    """
    from app.services.shot_cast import resolve_shot_cast
    return [cc.character.name for cc in resolve_shot_cast(db, shot).characters
            if cc.character.name]


def shot_subject_characters(db: Session, shot: Shot) -> list[Character]:
    """本镜「主体角色」权威定义（去重保序）。

    这是**唯一**的判定口径，注入器（reference_resolver）与生成前硬门禁
    （prompt_compiler.audit_reference_slots）必须同时使用它，否则会出现
    「注入器把配角放进 ref_character_N → 门禁判 identity_reference_leak → 整单 409」。

    构成（四者并集）：
    1. ``shot.character_id`` —— 解析阶段标注的主角色
    2. ``dialogue_segments`` 的说话人 —— 有台词就必然在画面里
    3. 提示词里显式 ``@引用`` 的角色 —— 作者显式点名要求一致性
    4. **结构化动作字段里出现的角色名** —— ``beats[].action/start_pose/end_pose``
       与 ``start_state`` / ``end_state`` 的 pose/position/holding 文本

    第 4 条是 2026-11-21 补的，修的是一个系统性漏角色事故：

    解析 schema 每个 shot 只有 ``character_id``（**主说话角色名，单值**），没有
    「本镜出场角色」数组。于是双人镜里第二个人物只出现在自由文本中——
    实测「海风与夏日」第 2 集第 3 镜：

        character_id = 江屿
        beats[0].action = 江屿双腿弯曲…落在干沙上
        beats[1].action = 林晚在江屿右侧并排坐下…     ← 林晚只在 beats 里
        dialogue_segments = []                        ← 无对白，第 2 条失效
        image_prompt 里没有 @林晚                      ← 第 3 条失效

    三个来源全部落空 → 主体角色只算出江屿 → 只绑 ``ref_identity`` 一个槽位
    → 生成出来的分镜图里出现「两个江屿」而不是江屿+林晚。前端详情页
    也正好显示 ``ref_character_2`` 为空「未绑定」。

    为什么用结构化字段而不是全文本子串匹配：``beats``/``state`` 是解析阶段
    专门标注「谁做了什么」的字段，比在整段提示词里搜名字精确得多，不会把
    「禁止出现林晚」这类负向约束误判成出场。项目内角色名精确匹配（非子串），
    且只在已存在的角色集合里比对，不凭空造角色。
    """
    from app.services.ref_inject import extract_refs

    ordered: list[Character] = []
    seen: set[str] = set()

    def _add(ch: Character | None) -> None:
        if ch is not None and ch.id not in seen:
            seen.add(ch.id)
            ordered.append(ch)

    if shot.character_id:
        _add(db.get(Character, shot.character_id))
    scene = db.get(Scene, shot.scene_id)
    project_id = scene.project_id if scene is not None else None
    for seg in (shot.dialogue_segments or []):
        if not isinstance(seg, dict):
            continue
        speaker_id = seg.get("speaker_id") or seg.get("character_id")
        if speaker_id:
            _add(db.get(Character, speaker_id))
            continue
        name = seg.get("speaker")
        if name:
            query = db.query(Character).filter(Character.name == name)
            if project_id:
                query = query.filter(Character.project_id == project_id)
            _add(query.first())
    # 提示词显式 @引用（含分镜图与视频提示词：两者可能由不同阶段渲染）
    mentioned: list[str] = []
    for text in (shot.image_prompt or "", shot.video_prompt or ""):
        for name in extract_refs(text):
            if name not in mentioned:
                mentioned.append(name)
    for name in mentioned:
        query = db.query(Character).filter(Character.name == name)
        if project_id:
            query = query.filter(Character.project_id == project_id)
        _add(query.first())
    # 结构化动作/状态字段里点名的角色（多角色镜的主要来源）
    for ch in _characters_from_structured_fields(db, shot, project_id):
        _add(ch)
    return ordered


def _shot_action_texts(shot: Shot) -> list[str]:
    """收集镜头里所有「描述谁做了什么」的结构化文本片段。

    只取动作/姿态/位置/持物类字段，不取 dialogue 正文——
    台词里被谈论的人不等于在画面里。

    P1 聚合改造：改为通过 shot_timeline() 访问器读取，旧数据自动回退 beats。
    """
    from app.services.shot_timeline import shot_timeline as _tl
    texts: list[str] = []
    for unit in _tl(shot):
        if not isinstance(unit, dict):
            continue
        # timeline 单元取 action；legacy beats 保留 action/start_pose/end_pose
        for key in ("action", "start_pose", "end_pose"):
            val = unit.get(key)
            if isinstance(val, str) and val:
                texts.append(val)
    for state in (shot.start_state, shot.end_state):
        if not isinstance(state, dict):
            continue
        for key in ("pose", "position", "holding"):
            val = state.get(key)
            if isinstance(val, str) and val:
                texts.append(val)
    return texts


def _characters_from_structured_fields(
    db: Session, shot: Shot, project_id: str | None
) -> list[Character]:
    """从 beats / start_state / end_state 的动作文本里解析出场角色。

    精确名匹配（角色名整体出现），按「文本中首次出现位置」排序，
    让主角色（character_id）之后的人物保持剧本叙述顺序，槽位分配稳定。
    """
    texts = _shot_action_texts(shot)
    if not texts:
        return []
    query = db.query(Character)
    if project_id:
        query = query.filter(Character.project_id == project_id)
    chars = [c for c in query.all() if c.name]
    if not chars:
        return []
    blob = "\n".join(texts)
    hits: list[tuple[int, Character]] = []
    for ch in chars:
        pos = blob.find(ch.name)
        if pos >= 0:
            hits.append((pos, ch))
    hits.sort(key=lambda x: x[0])
    return [ch for _pos, ch in hits]


def build_shot_asset_manifest(db: Session, shot: Shot) -> dict:
    """生成镜头资产清单：出场角色名列表 + 场景标题 + 道具名（显式 @引用 或提示词正文提及）。

    返回 {"characters": [名...], "scene": 标题或 None, "props": [名...]}，
    供任务快照与前端展示使用（镜头-资产可追踪引用关系）。
    """
    from app.services.ref_inject import extract_refs
    from app.services.shot_cast import resolve_shot_cast

    scene = db.get(Scene, shot.scene_id)
    prompt_text = shot.image_prompt or ""
    prop_names: list[str] = []
    cast = resolve_shot_cast(db, shot)
    if cast.source == "shot_plan":
        # 契约路径：可见道具以 shot_plan 为准（含 PropShotState 保护性合并结果）
        prop_names = [cp.prop.name for cp in cast.props if cp.prop.name]
    elif scene is not None and prompt_text:
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
    # 道具注入：有 shot_plan 契约时以 ShotCast 清单为准（与 reference_resolver 槽位
    # 绑定口径对齐，避免注入与绑定不一致导致 409）；无契约走生命周期状态驱动，
    # 状态缺失时降级启发式
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
    from app.services.shot_cast import resolve_shot_cast

    cast = resolve_shot_cast(db, shot)
    if cast.source == "shot_plan":
        # 契约路径：可见道具完全以 shot_plan 为准（resolve_shot_cast 已完成
        # PropShotState 保护性合并：absent 剔除、offscreen/occluded 不注入参考图）
        for cp in cast.props:
            prop = cp.prop
            if not prop.name or not prop.anchor_asset_id:
                continue
            if not cp.reference_required:
                # offscreen/occluded：保留在清单但不注入参考图与锁定约束
                continue
            if prop.name not in existing_refs:
                inject_names.append(prop.name)
            visible_props.append(prop.name)
            if prop.name in existing_refs:
                continue
            # 位置：优先契约 position，其次按交互方式通用推导
            desc = cp.position or _INTERACTION_DESC.get(cp.interaction)
            if not desc and cp.interaction and cp.interaction != "none":
                desc = cp.interaction
            seg = f"@{prop.name} 外观须与其参考图一致"
            if desc:
                seg += f"，{desc}"
            if cp.state is not None and cp.state.appearance_constraint:
                seg += f"，{cp.state.appearance_constraint}"
            constraint_lines.append(seg)
    elif scene is not None:
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
