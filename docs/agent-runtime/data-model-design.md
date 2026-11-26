# Agent Runtime 数据模型设计

> 基于 Phase 0 审计结果，对齐现有数据模型
> 原则: 不重复存储项目业务状态; 仅补充 Agent Runtime 所需

---

## 一、新增表

### 1. agent_goal

Agent 用户目标持久化。每个目标对应一个用户级任务意图。

| 列名 | 类型 | 说明 |
|------|------|------|
| id | VARCHAR(32) PK | gol_xxx |
| session_id | VARCHAR(32) NOT NULL | 归属会话 |
| project_id | VARCHAR(32) | 目标项目(可空) |
| description | TEXT NOT NULL | 目标描述 |
| goal_type | VARCHAR(32) DEFAULT 'user_request' | 目标类型 |
| constraints | JSON | 约束(allow_/require_human_review等) |
| success_criteria | JSON | 可验证完成标准列表 |
| status | VARCHAR(16) DEFAULT 'active' | active/paused/completed/failed/cancelled |
| created_at | DATETIME NOT NULL | |
| updated_at | DATETIME NOT NULL | |

### 2. agent_plan

执行计划，每个 Goal 可有多个 Revision。

| 列名 | 类型 | 说明 |
|------|------|------|
| id | VARCHAR(32) PK | pln_xxx |
| goal_id | VARCHAR(32) NOT NULL | gol_xxx |
| revision | INTEGER DEFAULT 1 | 计划版本 |
| plan_json | JSON | 计划快照(含 steps 列表) |
| status | VARCHAR(16) DEFAULT 'active' | active/superseded/completed/failed |
| created_at | DATETIME NOT NULL | |
| updated_at | DATETIME NOT NULL | |

### 3. agent_plan_step

计划步骤（重构自现有 agent_plan_step 表，扩展字段）。

| 列名 | 类型 | 说明 |
|------|------|------|
| id | VARCHAR(32) PK | stp_xxx |
| plan_id | VARCHAR(32) NOT NULL | pln_xxx |
| step_index | INTEGER DEFAULT 0 | 步骤序号 |
| step_type | VARCHAR(16) DEFAULT 'tool_call' | tool_call/clarification/reply/wait_approval |
| tool_name | VARCHAR(64) | 工具名 |
| description | VARCHAR(500) | 步骤描述 |
| args | JSON | 工具参数 |
| depends_on | JSON | [step_id...] 依赖 |
| preconditions | JSON | [{type, key, expected}] |
| verification | JSON | [{type, check}] |
| requires_approval | BOOLEAN DEFAULT FALSE | 是否需要人工审批 |
| status | VARCHAR(20) DEFAULT 'pending' | pending/ready/running/waiting_job/waiting_approval/waiting_user/completed/failed/skipped/cancelled |
| retry_count | INTEGER DEFAULT 0 | 已重试次数 |
| max_retries | INTEGER DEFAULT 3 | 最大重试次数 |
| timeout_seconds | INTEGER DEFAULT 600 | 超时时间 |
| started_at | DATETIME | 开始时间 |
| completed_at | DATETIME | 完成时间 |
| result | JSON | 执行结果 |
| error_code | VARCHAR(48) | 错误码 |
| error_message | TEXT | 错误信息 |
| created_at | DATETIME NOT NULL | |
| updated_at | DATETIME NOT NULL | |

### 4. agent_execution_log

执行审计日志。

| 列名 | 类型 | 说明 |
|------|------|------|
| id | VARCHAR(32) PK | ael_xxx |
| goal_id | VARCHAR(32) | 关联 Goal |
| plan_id | VARCHAR(32) | 关联 Plan |
| step_id | VARCHAR(32) | 关联 Step |
| tool_name | VARCHAR(64) | 工具名 |
| input | JSON | 输入 |
| output | JSON | 输出 |
| status | VARCHAR(16) NOT NULL | started/succeeded/failed/timeout |
| error_code | VARCHAR(48) | 错误码 |
| error_message | TEXT | 错误信息 |
| started_at | DATETIME NOT NULL | 开始时间 |
| completed_at | DATETIME | 结束时间 |
| duration_ms | INTEGER | 耗时(ms) |
| created_at | DATETIME NOT NULL | |

### 5. agent_event

Agent Runtime 持久化事件(outbox)。

| 列名 | 类型 | 说明 |
|------|------|------|
| event_seq | INTEGER PK AUTOINCREMENT | 全局递增 |
| goal_id | VARCHAR(32) | 关联 Goal |
| plan_id | VARCHAR(32) | 关联 Plan |
| step_id | VARCHAR(32) | 关联 Step |
| task_id | VARCHAR(32) | agent_task.id |
| job_id | VARCHAR(32) | 关联 Job |
| event_type | VARCHAR(48) NOT NULL | agent.goal.created/plan.created/step.started等 |
| payload | JSON | 事件负载 |
| created_at | DATETIME NOT NULL | |

### 6. agent_verification

验证记录。

| 列名 | 类型 | 说明 |
|------|------|------|
| id | VARCHAR(32) PK | avf_xxx |
| step_id | VARCHAR(32) NOT NULL | 关联 Step |
| verification_type | VARCHAR(32) NOT NULL | state/artifact/business/quality/human |
| status | VARCHAR(16) NOT NULL | passed/failed/partial/needs_review |
| checks | JSON | [{name, status, details}] |
| issues | JSON | [{reason, suggestion}] |
| created_at | DATETIME NOT NULL | |

---

## 二、现有表改动

### agent_task 扩展

```sql
ALTER TABLE agent_task ADD COLUMN goal_id VARCHAR(32);
ALTER TABLE agent_task ADD COLUMN plan_id VARCHAR(32);
ALTER TABLE agent_task ADD COLUMN current_step_id VARCHAR(32);
ALTER TABLE agent_task ADD COLUMN lifecycle_state VARCHAR(32) DEFAULT 'created';
ALTER TABLE agent_task ADD COLUMN progress_snapshot JSON;
ALTER TABLE agent_task ADD COLUMN resume_state JSON;
ALTER TABLE agent_task ADD COLUMN failure_reason TEXT;
```

### agent_plan_step 重构

- 现有 agent_plan_step 字段保留
- Migration 增加 depends_on / preconditions / verification / retry_count / max_retries / timeout_seconds 列
- 旧数据通过 plan_id 为 NULL 与新逻辑区分

---

## 三、关系总览

```
agent_session (保持不变)
    │
agent_goal (新增) → goal_id 关联
    │
agent_plan (新增) → goal_id/plan_id 关联
    │
agent_plan_step (重构) → plan_id 关联
    │
    ├── agent_execution_log (新增)
    ├── agent_verification (新增)
    └── agent_event (新增)
            │
agent_task (扩展) → 已有 task/agent_confirmation 关联不变
    │
    ├── agent_confirmation (保持不变)
    └── task (统一编排层, 保持不变)
            │
        task_stage (保持不变)
            │
        job (生产执行层, 保持不变)
```

---

## 四、Migration 原则

1. 所有新增列为 NULLABLE，不阻塞现有数据
2. 使用 Alembic，保持版本链连续
3. 新表不依赖已有业务数据，初创可为空
4. agent_goal/agent_plan/agent_plan_step 仅在 Agent Runtime 初始化时启用
5. 现有 agent_plan_step 数据保留，新逻辑通过 plan_id 字段区分新旧
6. agent_event 复用现有 JobEvent 的 autoincrement seq 机制
7. 所有 Migration 在 Phase 1 完成后才进入生产，Phase 0 仅设计不执行
