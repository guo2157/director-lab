# Agent Runtime 数据库迁移计划

> 基于 Phase 0 审计结果与 data-model-design.md
> 原则: 增量兼容, 非破坏性, 分阶段执行

---

## 一、迁移总览

| 阶段 | 对应 Phase | 范围 | 风险 |
|------|----------|------|------|
| M1 | Phase 1 | 新增表 + agent_task 扩展 | 低 (仅新增列+新表) |
| M2 | Phase 2 | agent_plan_step 重构 + agent_event | 中 (改现有表结构) |
| M3 | Phase 4 | Human Gate 审核版本绑定字段 | 中 (改业务表) |

---

## 二、M1: Agent Runtime 基础模型 (Phase 1)

### M1.1 新增表 (agent_goal / agent_plan / agent_execution_log)

agent_goal: id/session_id/project_id/description/goal_type/constraints/success_criteria/status/created_at/updated_at
agent_plan: id/goal_id/revision/plan_json/status/created_at/updated_at
agent_execution_log: id/goal_id/plan_id/step_id/tool_name/input/output/status/error_code/error_message/started_at/completed_at/duration_ms/created_at

### M1.2 agent_task 扩展

新增列: goal_id/plan_id/current_step_id/lifecycle_state/progress_snapshot/resume_state/failure_reason, 全部 NULLABLE

---

## 三、M2: Plan Step + Event + Verification (Phase 2)

### M2.1 agent_plan_step 扩展

新增: plan_id/description/depends_on/preconditions/verification/requires_approval/retry_count/max_retries/timeout_seconds/started_at/completed_at/error_message

### M2.2 agent_event 表

event_seq(自增PK)/goal_id/plan_id/step_id/task_id/job_id/event_type/payload/created_at

### M2.3 agent_verification 表

id/step_id/verification_type/status/checks/issues/created_at

---

## 四、M3: Human Gate 审核版本绑定 (Phase 4)

根据 Phase 4 审计结果补充 Migration。具体字段依赖现有 Asset/Shot 表实际审核列情况，在 Phase 4 设计阶段确定。

---

## 五、兼容性约束

1. 所有新增列 NULLABLE, 默认值安全
2. 不删除任何现有列
3. 不修改现有索引与约束
4. 不修改现有业务模型表 (agent_session/message/confirmation/production/job)
5. 旧 agent_plan_step 数据: plan_id=NULL 区分新旧, 新Runtime只读 plan_id 非空的step
6. Migration 链连续, 每次单独文件
7. 每次 Migration 包含 downgrade

---

## 六、回滚计划

| 阶段 | 回滚方式 |
|------|---------|
| M1 | 删除新增表 + DROP新增列 (不丢业务数据) |
| M2 | 同上, agent_plan_step旧数据 task_id 列仍在 |
| M3 | 待设计 |

所有回滚不影响现有业务功能: agent_task 原有字段不变, agent_plan_step 旧数据 task_id 仍在。
