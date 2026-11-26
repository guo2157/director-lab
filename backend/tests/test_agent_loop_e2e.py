"""Agent Loop 端到端功能验证（Phase 1+2 集成测试）。"""
import sys
import traceback

from sqlalchemy import create_engine
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


# 1. 连接数据库
try:
    engine = create_engine("sqlite:///./data/director_lab.db", echo=False)
    db = Session(engine)
    check("数据库连接", True)
except Exception as e:
    check("数据库连接", False, str(e))
    sys.exit(1)

test_session_id = "ags_testloop2026"
test_desc = "测试 Agent Loop 闭环：检查项目进度并生成概览报告"

# 2. Goal 创建/恢复
try:
    from app.services.goal_manager import get_or_create_goal, get_active_goal
    goal = get_or_create_goal(db, session_id=test_session_id, description=test_desc)
    check("Goal 创建", bool(goal.goal_id), f"ID: {goal.goal_id}, 状态: {goal.status}")

    goal2 = get_or_create_goal(db, session_id=test_session_id, description="更新的描述")
    check("Goal 复用", goal2.goal_id == goal.goal_id, "同 session 不创建新 Goal")
    check("Goal 描述更新", goal2.description == "更新的描述")
except Exception as e:
    check("Goal 流程", False, str(e))
    traceback.print_exc()

# 3. Context Snapshot
try:
    from app.services.context_resolver import snapshot as ctx_snapshot, build_context_prompt
    ctx = ctx_snapshot(db, session_id=test_session_id)
    check("Context Snapshot 生成", True, f"session={ctx.user.session_id}")
    prompt = build_context_prompt(ctx)
    check("Context Prompt 构建", isinstance(prompt, str))
except Exception as e:
    check("Context 流程", False, str(e))
    traceback.print_exc()

# 4. Constraint Check
try:
    from app.services.constraint_manager import check_constraints
    report = check_constraints(db, goal_constraints=goal.constraints)
    check("Constraint 检查", isinstance(report.passed, bool), f"passed={report.passed}")
except Exception as e:
    check("Constraint 流程", False, str(e))
    traceback.print_exc()

# 5. Plan 生成与保存
try:
    from app.services.planner import build_and_save_plan, get_plan_steps, get_next_pending_steps
    plan, report = build_and_save_plan(db, goal_id=goal.goal_id, description=test_desc)
    check("Plan 生成", bool(plan.plan_id), f"ID: {plan.plan_id}, steps: {len(plan.steps)}, revision: {plan.revision}")
    check("Plan 校验通过", report.passed, f"errors={report.errors}")
    for i, s in enumerate(plan.steps):
        check(f"Step {i+1} 工具存在", bool(s.tool_name), f"{s.step_key} -> {s.tool_name}")

    steps = get_plan_steps(db, plan.plan_id)
    check("Plan 持久化", len(steps) == len(plan.steps), f"DB: {len(steps)}, 内存: {len(plan.steps)}")

    pending = get_next_pending_steps(db, plan.plan_id)
    check("待执行步骤获取", len(pending) > 0, f"count={len(pending)}")
except Exception as e:
    check("Plan 流程", False, str(e))
    traceback.print_exc()

# 6. Execution Log 写入
try:
    from app.services.agent_runtime import log_step_start, log_step_end, StepResult, get_execution_logs
    from datetime import datetime

    step_id = steps[0]["id"] if steps else None
    tool_name = plan.steps[0].tool_name if plan.steps else "unknown"
    start = datetime.now()

    log_id = log_step_start(db, goal_id=goal.goal_id, plan_id=plan.plan_id,
                            step_id=step_id, tool_name=tool_name,
                            input_data={"description": test_desc})
    check("开始日志写入", bool(log_id))

    end_log = log_step_end(db, goal_id=goal.goal_id, plan_id=plan.plan_id,
                           step_id=step_id, tool_name=tool_name,
                           result=StepResult.SUCCESS,
                           output_data={"status": "mock_success"},
                           started_at=start)
    check("结束日志写入", bool(end_log))

    logs = get_execution_logs(db, goal_id=goal.goal_id, limit=10)
    check("日志查询", len(logs) >= 2, f"共 {len(logs)} 条")
except Exception as e:
    check("日志流程", False, str(e))
    traceback.print_exc()

# 7. Verifier 验证
try:
    from app.services.verifier import verify_step, verify_plan_completion
    if step_id:
        v = verify_step(db, step_id=step_id, tool_name=tool_name,
                        result_data={"status": "ok"})
        check("步骤验证", isinstance(v.verified, bool), f"verified={v.verified}, overall={v.overall}")

    pc = verify_plan_completion(db, plan_id=plan.plan_id)
    check("Plan 完成度检查", isinstance(pc["all_verified"], bool), f"all={pc['all_verified']}, total={pc['total']}")
except Exception as e:
    check("Verifier 流程", False, str(e))
    traceback.print_exc()

# 8. Replanner 错误分类
try:
    from app.services.replanner import classify_error, ErrorContext, ReplanAction
    ctx1 = ErrorContext(step_id="fake", error_code="TimeoutError", retry_count=1)
    check("瞬态错误分类为 RETRY", classify_error(ctx1) == ReplanAction.RETRY)

    ctx2 = ErrorContext(step_id="fake", error_code="ToolNotFound")
    check("工具错误分类为 REPLAN", classify_error(ctx2) == ReplanAction.REPLAN)

    ctx3 = ErrorContext(step_id="fake", error_code="TimeoutError", retry_count=5)
    check("超限错误分类为 REPLAN", classify_error(ctx3) == ReplanAction.REPLAN)
except Exception as e:
    check("Replanner 流程", False, str(e))
    traceback.print_exc()

# 9. Goal 状态查询
try:
    from app.services.agent_runtime import get_goal_status
    gs = get_goal_status(db, test_session_id)
    check("Goal 状态查询", gs["has_active_goal"], f"plan={gs.get('latest_plan')}")
except Exception as e:
    check("状态查询流程", False, str(e))
    traceback.print_exc()

# 汇总
print("\n" + "=" * 60)
print(f"验证结果: {passed} 通过 / {failed} 失败 / {passed + failed} 总计")
print("=" * 60)

# 清理
db.rollback()
db.close()

if failed > 0:
    sys.exit(1)
