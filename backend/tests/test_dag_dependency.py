"""DAG 依赖解析与分层就绪验证（Executor 集成）。"""
import sys
import traceback

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS: {name}")
        if detail:
            print(f"       {detail}")
    else:
        failed += 1
        print(f"  FAIL: {name}")
        if detail:
            print(f"       {detail}")


engine = create_engine("sqlite:///./data/director_lab.db", echo=False)
db = Session(engine)

session_id = "ags_dagtest2026"
# 覆盖查询 + 分析 + 生成三类意图，生成 6 步 DAG
desc = "检查项目进度概览并分析生成分镜图"

from app.models.agent import AgentPlanStep
from app.services.goal_manager import get_or_create_goal
from app.services.planner import (
    build_and_save_plan,
    get_next_pending_steps,
    get_plan_steps,
)

try:
    goal = get_or_create_goal(db, session_id=session_id, description=desc)
    plan, report = build_and_save_plan(db, goal_id=goal.goal_id, description=desc)
    db.commit()
    check("Plan 生成并持久化", report.passed and len(plan.steps) == 6,
          f"steps={len(plan.steps)}, passed={report.passed}")

    steps = get_plan_steps(db, plan.plan_id)
    check("步骤全部落库", len(steps) == 6, f"DB={len(steps)}")

    # 校验 depends_on 落库的是 id 而非 step_key
    rows = db.scalars(
        select(AgentPlanStep).where(AgentPlanStep.plan_id == plan.plan_id)
    ).all()
    all_ids = {r.id for r in rows}
    deps_are_ids = True
    dep_detail = []
    for r in rows:
        for d in (r.depends_on or []):
            if d not in all_ids:
                deps_are_ids = False
                dep_detail.append(f"{r.tool_name}.depends_on={d} 不在步骤 id 集合中")
    check("depends_on 已翻译为步骤 id", deps_are_ids,
          "无效依赖: " + "; ".join(dep_detail) if dep_detail else "全部依赖均为有效步骤 id")

    # 第 1 层：无依赖根步骤（project_overview 与 overview_check 两步，tool_name 同为 project_overview）
    root_ids = {r.id for r in rows if not r.depends_on}
    layer1 = get_next_pending_steps(db, plan.plan_id)
    layer1_ids = {s["id"] for s in layer1}
    check("第 1 层就绪步骤数", len(layer1) == 2, f"count={len(layer1)}")
    check("第 1 层恰为全部无依赖根步骤", layer1_ids == root_ids,
          f"layer1={len(layer1_ids)}, roots={len(root_ids)}")

    # 模拟执行第 1 层的 project_overview 步骤（置为 succeeded，不真实调用工具）
    po_step = next(r for r in rows if r.tool_name == "project_overview" and r.depends_on is None)
    po_step.status = "succeeded"
    db.flush()

    layer2 = get_next_pending_steps(db, plan.plan_id)
    layer2_ids = {s["id"] for s in layer2}
    layer2_tools = sorted(s["tool_name"] for s in layer2)
    check("执行 project_overview 后第 2 层解锁", "asset_stats" in layer2_tools and "list_jobs" in layer2_tools,
          str(layer2_tools))
    remaining_root = root_ids - {po_step.id}
    check("第 2 层仍包含剩余根步骤 overview_check", remaining_root.issubset(layer2_ids),
          f"剩余根是否全部就绪: {remaining_root.issubset(layer2_ids)}")

    # 模拟执行 overview_check
    oc_step = next(r for r in rows if r.tool_name == "project_overview" and r.depends_on is None and r.status == "pending")
    oc_step.status = "succeeded"
    db.flush()

    # 模拟执行第 2 层 asset_stats
    as_step = next(r for r in rows if r.tool_name == "asset_stats")
    as_step.status = "succeeded"
    db.flush()

    layer3 = get_next_pending_steps(db, plan.plan_id)
    layer3_tools = sorted(s["tool_name"] for s in layer3)
    check("依赖满足后下游按层推进", "list_jobs" in layer3_tools or "get_parse_status" in layer3_tools,
          f"第 3 层: {layer3_tools}")

    # 校验 list_jobs 与 get_parse_status 仅依赖 project_overview（已满足），应已就绪
    check("list_jobs 就绪（依赖 project_overview 已 succeeded）", "list_jobs" in layer3_tools, str(layer3_tools))
    check("get_parse_status 就绪（依赖 project_overview 已 succeeded）", "get_parse_status" in layer3_tools, str(layer3_tools))

    # 生成步骤 generate_default 依赖 overview_check，overview_check 已 succeeded，应就绪
    check("generate_default 就绪（依赖 overview_check 已 succeeded）", "request_generate" in layer3_tools, str(layer3_tools))

except Exception as e:
    check("DAG 验证流程", False, str(e))
    traceback.print_exc()
finally:
    db.rollback()
    db.close()

print("\n" + "=" * 60)
print(f"DAG 验证结果: {passed} 通过 / {failed} 失败 / {passed + failed} 总计")
print("=" * 60)

if failed > 0:
    sys.exit(1)
