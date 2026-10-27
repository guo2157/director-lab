"""镜头资产清单与参考绑定（P2）：分镜图生成时按镜头实际内容自动绑定参考资产。

设计约定（对应分镜一致性专项改造第六/七节）：
- 镜头资产清单来源：出场角色（character_id + dialogue_segments 说话人）+ 所属场景（scene_id）
  + 提示词中已显式 @引用 的道具
- 分镜图生成不能只依赖提示词中恰好写了 @名称：清单内未在提示词出现的角色/场景，
  以『参考资产』段自动补入提示词头部，使 @引用解析（ref_inject）能携带参考图
- 不相关的资产不传入：只注入镜头实际出场实体，不把项目全部资产灌入
- 道具不自动注入：道具是否入画由提示词显式 @引用决定，避免无关道具混入分镜
"""
from sqlalchemy.orm import Session
from app.models import Character, Scene, Shot


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
    """生成镜头资产清单：出场角色名列表 + 场景标题 + 提示词中已 @引用 的道具名。

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
    return {
        "characters": collect_shot_characters(db, shot),
        "scene": scene.title if scene is not None else None,
        "props": prop_names,
    }


def inject_asset_refs(db: Session, shot: Shot, prompt: str) -> str:
    """把镜头资产清单中未在提示词出现的角色/场景以 @引用 补入提示词头部。

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
    if not inject_names:
        return prompt
    ref_line = "参考资产：" + " ".join(f"@{n}" for n in inject_names)
    return f"{ref_line}\n{prompt}"
