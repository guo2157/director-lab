"""Agent Loop 主循环端到端集成测试（run_agent_loop 完整编排）。

验证 run_agent_loop 能从用户描述出发，完成 A-G 七阶段编排，
不因工具执行失败而崩溃。Phase 1 里程碑验证。
"""
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


engine = create_engine("sqlite:///./data/director_lab.db", echo=False)
db = Session(engine)

try:
    check("数据库连接", True)

    from app.services.agent_runtime import (
        run_agent_loop,
        AgentLoopResult,
        _MAX_LOOP_ITERATIONS,
    )

    # ── 场景 1：简单查询任务（预期 2-6 步 DAG）──
    session_id = "ags_mainloop_test1"
    desc1 = "检查项目进度概览"

    result1 = run_agent_loop(
        db,
        session_id=session_id,
        description=desc1,
    )
    check("场景1：查询返回 AgentLoopResult", isinstance(result1, AgentLoopResult))
    check("场景1：goal_id 已赋值", bool(result1.goal_id),
          f"goal_id={result1.goal_id}")
    check("场景1：plan_id 已赋值", bool(result1.plan_id),
          f"plan_id={result1.plan_id}")
    check("场景1：context_snapshot 已构建", isinstance(result1.context_snapshot, dict),
          f"keys: {list(result1.context_snapshot.keys())[:5] if result1.context_snapshot else 'none'}")
    check("场景1：状态非空", bool(result1.status),
          f"status={result1.status}")
    check("场景1：total_steps > 0", result1.total_steps > 0,
          f"total_steps={result1.total_steps}")
    check("场景1：message 非空", bool(result1.message),
          f"message={result1.message[:80]}")

    # 查询任务通常只读工具会因缺少 project_id 而失败
    # 但循环本身不应崩溃，fail/total 统计应合理
    # waiting 状态下还有 pending 步骤，不强行要求 completed+failed+skipped==total
    executed = result1.completed_steps + result1.failed_steps + result1.skipped_steps
    check("场景1：已完成步骤 ≤ 总步数",
          executed <= result1.total_steps and result1.completed_steps >= 1,
          f"c={result1.completed_steps} f={result1.failed_steps} s={result1.skipped_steps} t={result1.total_steps}, executed={executed}")

    # ── 场景 2：Goal 复用 ──
    result2 = run_agent_loop(
        db,
        session_id=session_id,
        description="更新后的概览查询",
    )
    check("场景2：复用同一 goal_id", result2.goal_id == result1.goal_id,
          f"{result2.goal_id} == {result1.goal_id}")
    check("场景2：复用同一 plan_id（同 goal 单 plan）",
          result2.plan_id == result1.plan_id,
          f"{result2.plan_id} == {result1.plan_id}")

    # ── 场景 3：含分析意图的任务（more steps）──
    session_id3 = "ags_mainloop_test3"
    desc3 = "分析项目进度并诊断问题"
    result3 = run_agent_loop(
        db,
        session_id=session_id3,
        description=desc3,
    )
    check("场景3：分析意图生成更多步骤",
          result3.total_steps >= 3,
          f"total_steps={result3.total_steps}")
    check("场景3：状态可判定", result3.status in ("completed", "failed", "waiting", "waiting_approval", "waiting_job", "blocked"),
          f"status={result3.status}")

    # ── 场景 4：约束检查 ──
    result4 = run_agent_loop(
        db,
        session_id="ags_mainloop_test4",
        description="检查项目状态",
        constraints={"max_cost": 100, "allowed_tools": ["read"]},
    )
    check("场景4：带上约束不会崩溃", isinstance(result4, AgentLoopResult))

    # ── 场景 5：空描述 fallback ──
    result5 = run_agent_loop(
        db,
        session_id="ags_mainloop_test5",
        description="随便看看",
    )
    # 默认 fallback 生成 1 步 default_overview
    check("场景5：模糊描述也有 plan", result5.total_steps >= 1,
          f"total_steps={result5.total_steps}")

    print("\n--- 执行摘要 ---")
    for label, r in [
        ("查询", result1),
        ("复用", result2),
        ("分析", result3),
        ("约束", result4),
        ("fallback", result5),
    ]:
        print(f"  {label}: status={r.status} steps=c{r.completed_steps}/f{r.failed_steps}/t{r.total_steps} goal={r.goal_id[:16]}...")

except Exception as e:
    check("主循环集成测试", False, str(e))
    traceback.print_exc()
finally:
    db.rollback()
    db.close()

print("\n" + "=" * 60)
print(f"Agent Loop 集成测试: {passed} 通过 / {failed} 失败 / {passed + failed} 总计")
print("=" * 60)

if failed > 0:
    sys.exit(1)
