"""参考图槽位四层审计（2026-11-24）。

对每一镜逐层核对「参考图页面 / 提示词 / 依赖的参考图 / 参考图槽位 / 已生成参考图」：
  ① 权威函数 shot_subject_characters 算出的主体角色
  ② 依赖落库 AssetPlanDependency 的槽位绑定
  ③ 详情页接口 GET /shots/{id}/regenerate/config 回显的 refs
  ④ 提示词文本里的 <imageN> 职责段 与「参考图职责」声明
  ⑤ 实际提交 job 的 __media__ / params.ref_* 与已生成资产文件是否存在、是否 current

用法：cd backend && .venv/bin/python ../scripts/audit_ref_slots.py <project_id> <episode>
"""
from __future__ import annotations

import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from app.core.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    Asset,
    AssetPlan,
    AssetPlanDependency,
    Job,
    Prop,
    Scene,
    Shot,
)
from app.services.shot_refs import shot_subject_characters  # noqa: E402

REF_SLOT_RE = re.compile(r"^ref_(identity|character_\d+|scene|prop(_\d+)?|image|prev_frame)$")
IMAGE_DUTY_RE = re.compile(r"^- <image(\d+)>（([^）]+)）：@([^\s—]+)", re.M)


def _deps_of(db, shot_id: str, plan_type: str = "keyframe") -> dict[str, dict]:
    """返回 {slot: {owner_id, asset_key, name}}"""
    plan = (
        db.query(AssetPlan)
        .filter(AssetPlan.owner_id == shot_id, AssetPlan.plan_type == plan_type)
        .first()
    )
    if plan is None:
        return {}
    out: dict[str, dict] = {}
    for d in db.query(AssetPlanDependency).filter(AssetPlanDependency.plan_id == plan.id):
        owner_id = None
        asset_key = None
        if d.ref_plan_id:
            rp = db.get(AssetPlan, d.ref_plan_id)
            if rp is not None:
                owner_id, asset_key = rp.owner_id, rp.asset_key
        out[d.slot] = {"owner_id": owner_id, "asset_key": asset_key}
    return out


def _resolve_asset(db, plan: AssetPlan | None) -> Asset | None:
    if plan is None:
        return None
    if plan.asset_id:
        a = db.get(Asset, plan.asset_id)
        if a is not None:
            return a
    if plan.owner_type == "shot" and plan.owner_id:
        return (
            db.query(Asset)
            .join(Job, Job.id == Asset.source_job_id)
            .filter(
                Job.shot_id == plan.owner_id,
                Asset.asset_type == plan.plan_type,
                Asset.validity == "current",
                Job.status == "succeeded",
            )
            .order_by(Asset.version.desc())
            .first()
        )
    return None


def audit(project_id: str, episode: int) -> int:
    db = SessionLocal()
    problems: list[str] = []
    try:
        rows = (
            db.query(Shot, Scene)
            .join(Scene, Scene.id == Shot.scene_id)
            .filter(Scene.project_id == project_id, Shot.episode == episode)
            .order_by(Scene.seq, Shot.seq)
            .all()
        )
        props = {p.name: p for p in db.query(Prop).filter(Prop.project_id == project_id).all()}

        print(f"项目 {project_id} 第 {episode} 集，共 {len(rows)} 镜\n")

        for shot, scene in rows:
            tag = f"[场{scene.seq}-镜{shot.seq}]"
            subs = [c.name for c in shot_subject_characters(db, shot)]
            deps = _deps_of(db, shot.id)

            # 槽位分类
            char_slots = {k: v for k, v in deps.items()
                          if k == "ref_identity" or k.startswith("ref_character_")}
            scene_slots = {k: v for k, v in deps.items() if k == "ref_scene"}
            prop_slots = {k: v for k, v in deps.items()
                          if k.startswith("ref_prop")}

            # ① vs ② 角色数量
            if len(subs) != len(char_slots):
                problems.append(
                    f"{tag} ①权威角色 {subs}（{len(subs)}）≠ ②依赖角色槽 {list(char_slots)}（{len(char_slots)}）"
                )

            # 槽位 → 角色名解析
            slot_names: dict[str, str] = {}
            for slot, info in deps.items():
                oid = info.get("owner_id")
                if oid and oid.startswith("chr_"):
                    from app.models import Character
                    ch = db.get(Character, oid)
                    slot_names[slot] = ch.name if ch else f"<{oid}>"
                elif oid and oid.startswith("scn_"):
                    sc = db.get(Scene, oid)
                    slot_names[slot] = sc.title if sc else f"<{oid}>"
                elif oid and oid.startswith("prp_"):
                    pr = db.get(Prop, oid)
                    slot_names[slot] = pr.name if pr else f"<{oid}>"

            # ③ 详情页接口回显（直接调服务层，等价于 HTTP）
            plan = (
                db.query(AssetPlan)
                .filter(AssetPlan.owner_id == shot.id, AssetPlan.plan_type == "keyframe")
                .first()
            )
            kf_asset = _resolve_asset(db, plan)
            ui_refs: list[str] = []
            ui_prompt = ""
            if kf_asset is not None:
                try:
                    from app.services import regeneration as regen

                    cfg = regen.build_base_config(db, kf_asset)
                    ui_refs = [r.get("slot_param") for r in (cfg.get("refs") or [])]
                    ui_prompt = (cfg.get("prompt") or {}).get("rendered") or ""
                except Exception as e:  # noqa: BLE001
                    problems.append(f"{tag} ③详情页配置读取失败: {e}")

            if ui_refs and set(ui_refs) != set(deps):
                problems.append(
                    f"{tag} ③详情页回显槽 {sorted(ui_refs)} ≠ ②依赖槽 {sorted(deps)}"
                )

            # ④ 提示词：<imageN> 职责段 vs 实际槽位；以及正文里的「只有X一个人物」
            prompt = (plan.prompt if plan else "") or ""
            duty = IMAGE_DUTY_RE.findall(prompt)
            duty_names = [n for _no, _kind, n in duty]
            if prompt:
                if len(subs) > 1 and "画面中只有" in prompt:
                    only = prompt.split("画面中只有 ")[1].split(" 一个人物")[0]
                    problems.append(
                        f"{tag} ④双人镜提示词仍写「画面中只有 {only} 一个人物」"
                    )
                # 角色/场景名是否都出现在职责段
                missing = [n for n in subs if n not in duty_names]
                if missing:
                    problems.append(f"{tag} ④提示词职责段缺角色 {missing}（实际 {duty_names}）")
                if scene.title and scene.title not in duty_names:
                    problems.append(f"{tag} ④提示词职责段缺场景 {scene.title}（实际 {duty_names}）")
                # 道具：正文提到但无锚定图槽位
                mentioned_props = [n for n in props if n in prompt]
                missing_prop_slots = [n for n in mentioned_props if n not in slot_names.values()]
                if missing_prop_slots and not prop_slots:
                    problems.append(
                        f"{tag} ④正文提到道具 {missing_prop_slots}，但②无 ref_prop 槽位、提示词职责段无道具行"
                    )

            # ⑤ 已生成参考图文件是否存在 / validity
            for slot, info in deps.items():
                if not REF_SLOT_RE.match(slot):
                    continue
                ref_plan = None
                if info.get("asset_key"):
                    ref_plan = (
                        db.query(AssetPlan)
                        .filter(
                            AssetPlan.project_id == project_id,
                            AssetPlan.asset_key == info["asset_key"],
                        )
                        .first()
                    )
                a = _resolve_asset(db, ref_plan)
                if a is None:
                    problems.append(f"{tag} ⑤槽位 {slot}({slot_names.get(slot,'?')}) 无已生成资产")
                    continue
                if a.validity != "current":
                    problems.append(
                        f"{tag} ⑤槽位 {slot}({slot_names.get(slot,'?')}) 资产 {a.id} validity={a.validity}"
                    )
                fp = a.file_path or ""
                if not fp:
                    problems.append(f"{tag} ⑤槽位 {slot} 资产 {a.id} file_path 为空")
                elif not os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend", fp)):
                    problems.append(f"{tag} ⑤槽位 {slot} 资产 {a.id} 文件不存在: {fp}")

            # ⑥ 实际 job 下发的 __media__
            job = (
                db.query(Job)
                .filter(Job.shot_id == shot.id, Job.job_type == "generate_keyframe",
                        Job.status == "succeeded")
                .order_by(Job.finished_at.desc())
                .first()
            )
            media = {}
            if job is not None:
                media = (job.params or {}).get("__media__") or {}
                if media and set(media) != set(deps):
                    problems.append(
                        f"{tag} ⑥下发 __media__ {sorted(media)} ≠ ②依赖槽 {sorted(deps)}"
                    )
                jp = (job.params or {}).get("prompt") or ""
                if jp and jp != prompt:
                    # 提示词文本不一致（页面所见 ≠ 实际下发）
                    h1 = jp.split("\n")[0][:40]
                    h2 = prompt.split("\n")[0][:40]
                    if h1 != h2:
                        problems.append(
                            f"{tag} ⑥页面提示词首行与下发不一致：页面「{h2}」/ 下发「{h1}」"
                        )

            print(
                f"{tag} {scene.title}/{shot.seq} 角色={subs} 槽={sorted(deps)} "
                f"道具槽={list(prop_slots)} media={sorted(media)}"
            )

        print("\n" + "=" * 70)
        if problems:
            print(f"发现 {len(problems)} 处不一致：\n")
            for p in problems:
                print(" ✗", p)
            return 1
        print("✅ 六层全部一致")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    sys.exit(audit(sys.argv[1], int(sys.argv[2])))
