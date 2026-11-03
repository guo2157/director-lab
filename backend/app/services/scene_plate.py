# -*- coding: utf-8 -*-
"""场景板 LLM 编译器（P1）：把剧情 description 降维成空镜环境规格 plate。

分层策略（规则 + LLM 双保险）：
- 第一层：LLM 编译器（scene_plate_compiler.md 系统提示词，temperature=0.2，JSON 模式），
  输出经 validate_plate 校验，两次不过落规则清洗兜底，不阻塞生成；
- 第二层：_strip_story_content 规则清洗（LLM 失败/未配置/输出不合格时兜底）。

落库约定：scene.plate 存完整 JSON，scene.plate_text 存四行文本；
不覆盖 description（分镜层仍需要 beat）。同一场景只编译一次（plate_text 非空即复用）。
地点圣经（基础版）：同项目内同 location_id 的场景复用已生成的 anchors。
"""
import json
import logging
import re

from sqlalchemy.orm import Session

from app.integrations.llm import LLMClient, LLMError
from app.models import Scene
from app.models.asset_plan import AssetPlan
from app.services.prompt_store import get_prompt

logger = logging.getLogger(__name__)

PROMPT_NAME = "scene_plate_compiler"

# 校验规则（规范第 7 节）
BANNED = ("不要", "不能有", "禁止", "避免", "没有人物", "无人物", "无人影",
          "no people", "no person", "without", "exclude")
MOTION = ("推近", "拉远", "摇镜", "移镜", "晃动", "慢镜头", "zoom", "pan", "dolly")
REQUIRED = ("location_id", "location_name", "time_of_day", "weather",
            "foreground", "midground", "background", "standable_area",
            "light", "camera", "elements", "anchors", "plate_text")


def validate_plate(p: dict) -> list[str]:
    """校验 LLM 输出的 plate JSON，返回错误列表（空列表即合格）。"""
    errs = [f"缺字段 {k}" for k in REQUIRED if not p.get(k)]
    t = p.get("plate_text", "")
    if len([l for l in t.splitlines() if l.strip()]) != 4:
        errs.append("plate_text 必须四行")
    for w in BANNED:
        if w.lower() in t.lower():
            errs.append(f"否定词：{w}")
    for w in MOTION:
        if w in t:
            errs.append(f"运镜词：{w}")
    if "空无一人" not in t:
        errs.append("缺「空无一人」")
    if "整个画面只由" not in t:
        errs.append("缺「整个画面只由…构成」")
    if not re.search(r"\d+mm", t):
        errs.append("缺焦段")
    if not any(d in p.get("light", {}).get("direction", "")
               for d in ("左", "右", "前", "后", "顶", "上方", "侧")):
        errs.append("光向不明确")
    if len(p.get("elements") or []) < 3:
        errs.append("elements 少于 3 项")
    return errs


def _location_bible(db: Session, project_id: str, place_key: str | None) -> str:
    """基础版地点圣经：同项目内同一地点的 anchors 复用。

    place_key 取 Scene.normalized_name（地点+时段复合键）。Scene 表本身没有
    location_id 列，地点标识只存在于 plate JSON 内，因此匹配时同时兼容
    plate.location_id 与 normalized_name。修复前调用点恒传 None，地点圣经从未生效。
    """
    if not place_key:
        return "（无，请在本条内首次定义锚点）"
    rows = (
        db.query(Scene)
        .filter(Scene.project_id == project_id, Scene.plate.isnot(None))
        .all()
    )
    for s in rows:
        try:
            plate = json.loads(s.plate) if isinstance(s.plate, str) else (s.plate or {})
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(plate, dict) or not plate.get("anchors"):
            continue
        same_place = plate.get("location_id") == place_key or (
            s.normalized_name and s.normalized_name == place_key
        )
        if same_place:
            return json.dumps({place_key: {
                "location_name": plate.get("location_name", ""),
                "anchors": plate.get("anchors", []),
            }}, ensure_ascii=False)
    return "（无，请在本条内首次定义锚点）"


def _call_llm(title: str, description: str, style: str, location_bible: str, aspect: str) -> dict:
    """调用 LLM 编译器：两次校验不过抛 LLMError，由调用方兜底。"""
    system_prompt = get_prompt(PROMPT_NAME)
    user_prompt = (
        f"场景标题：{title}\n"
        f"剧情描述：{description}\n"
        f"项目美术风格：{style or '未指定，使用中性写实风格'}\n"
        f"地点圣经：{location_bible}\n"
        f"画幅：{aspect}"
    )
    client = LLMClient()
    last_errs: list[str] = []
    for _ in range(2):
        out = client.chat_json(system_prompt, user_prompt)
        errs = validate_plate(out)
        if not errs:
            return out
        last_errs = errs
        logger.warning("scene_plate LLM 输出不合格: %s", errs)
    raise LLMError("llm_invalid_output", f"场景板编译输出两次不合格: {last_errs}")


def compile_scene_plate(db: Session, scene: Scene, *, style: str | None = None,
                        aspect: str = "9:16", persist_fallback: bool = True) -> dict:
    """编译场景板：plate_text 已有则直接复用（懒编译 + 落库缓存）。

    返回 {"plate": dict|None, "plate_text": str, "source": "cached|llm|rules|rules_transient"}。
    LLM 失败时回退规则清洗（_strip_story_content），保证生成链路不阻塞。

    persist_fallback=False 时规则兜底不写库（source=rules_transient）：
    供自动化入口使用，避免无 LLM 配置时把粗清洗的 description 固化成 plate_text，
    从而在不经意间改变既有场景提示词的走向。
    """
    # 1) 缓存命中
    if scene.plate_text:
        plate = None
        if scene.plate:
            try:
                plate = json.loads(scene.plate) if isinstance(scene.plate, str) else scene.plate
            except (json.JSONDecodeError, TypeError):
                plate = None
        return {"plate": plate, "plate_text": scene.plate_text, "source": "cached"}

    description = scene.description or ""
    # 2) LLM 编译（两次校验不过/未配置/超时 → 规则兜底）
    try:
        bible = _location_bible(db, scene.project_id, None)
        plate = _call_llm(scene.title or "", description, style or "", bible, aspect)
        # 基础版地点圣经：落库后同地点场景可复用 anchors
        scene.plate = json.dumps(plate, ensure_ascii=False)
        scene.plate_text = plate["plate_text"]
        db.commit()
        # 回写 asset_plan.prompt，使 UI 显示与实际生成使用的提示词一致
        db.query(AssetPlan).filter(
            AssetPlan.owner_type == "scene",
            AssetPlan.owner_id == scene.id,
        ).update({"prompt": plate["plate_text"]}, synchronize_session=False)
        db.commit()
        return {"plate": plate, "plate_text": plate["plate_text"], "source": "llm"}
    except (LLMError, Exception) as e:  # noqa: BLE001 - 任何 LLM 故障都兜底，不阻塞生成
        logger.warning("scene_plate LLM 编译失败，回退规则清洗: %s", e)
    # 3) 规则兜底：_strip_story_content 清洗后的 description 作为 plate_text
    from app.services.prompt_compiler import _strip_story_content
    fallback = _strip_story_content(description) or "室外自然环境"
    if not persist_fallback:
        # 不落库：只把兜底文本返回给调用方判断，不改变 Scene 与 AssetPlan 的持久状态。
        # 无 LLM 配置时这条路径会被反复走到，落库会让 description 的粗清洗结果
        # 固化成正式场景板，后续即使配好 LLM 也不会再重新编译（plate_text 非空即缓存命中）。
        return {"plate": None, "plate_text": fallback, "source": "rules_transient"}
    scene.plate_text = fallback
    db.commit()
    # 回写 asset_plan.prompt，使 UI 显示与实际生成使用的提示词一致
    db.query(AssetPlan).filter(
        AssetPlan.owner_type == "scene",
        AssetPlan.owner_id == scene.id,
    ).update({"prompt": fallback}, synchronize_session=False)
    db.commit()
    return {"plate": None, "plate_text": fallback, "source": "rules"}
