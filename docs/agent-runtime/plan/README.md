# Agent Runtime 升级计划（2026-11-26）

本目录保存 Director-Lab Agent Runtime 升级的最终 Plan，以及交给编码 Agent（Codex / Claude Code / Kiro 等）逐条执行的任务提示词。

- 基线：`guo2157/director-lab` 默认分支，tree `1df9fe8b`（2026-11-26）
- 结论：原始升级 Plan 方向正确但已过时——Phase 0 审计与 Phase 1/2 骨架代码已入库，但新 Runtime 没有接入主对话链路，且有 13 处 P0 缺陷。最终 Plan 改为"先修错 → 接入 → 收敛 → 再扩展"。
- 进度勾选：[../implementation-plan.md](../implementation-plan.md)

## 文件

| 文件 | 内容 |
|---|---|
| [final-plan.md](final-plan.md) | 最终 Plan：原 Plan 的问题、最新代码审计（P0 缺陷清单）、目标架构、六个阶段、硬性约束、已确认决策 |
| [phase-a-agent-prompts.md](phase-a-agent-prompts.md) | Phase A · 止血与正确性（14 个任务） |
| [phase-b-agent-prompts.md](phase-b-agent-prompts.md) | Phase B · 最小可用版本：`/chat` 接入、审批 / Job 后自动继续、重启恢复（12 个任务） |
| [phase-c-agent-prompts.md](phase-c-agent-prompts.md) | Phase C · 状态驱动 Planner、统一工具目录、目标级预算（11 个任务） |
| [phase-d-agent-prompts.md](phase-d-agent-prompts.md) | Phase D · 审核开关、审核绑定版本、Worker 兜底（11 个任务） |
| [phase-e-agent-prompts.md](phase-e-agent-prompts.md) | Phase E · Agent 工作台前端（11 个任务） |
| [phase-f-agent-prompts.md](phase-f-agent-prompts.md) | Phase F · 追踪、计量、记忆、指标、评测、后端 CI、清理与最终验收（11 个任务） |

## 怎么用

1. 按 A → F 的顺序执行；上一阶段的验收测试全绿才能开始下一阶段（例外：F6 后端 CI 可以提前到任何时候做）。
2. 每次给编码 Agent 开新会话时，先粘贴该阶段文件开头的「〇、通用上下文」，再粘贴**一个**任务；一个任务对应一个 PR。
3. 阶段内的执行顺序以各文件开头的说明为准（例如 Phase A 是 A0 → A12a → A1 … A11 → A12b）。
4. 任务完成后，在 [../implementation-plan.md](../implementation-plan.md) 中勾选；阶段结束时填写"阶段完成记录"。
5. 提示词中的行号以基线版本为准，代码变动后请按函数名定位；提示词由阅读代码写成，未在本地运行验证，执行中发现与实际代码不符时，以代码为准并在 PR 中说明。

## 执行顺序

```
A 止血（P0 全修 + 测试隔离）
  └─► B 最小可用版本：主链路接入 + Worker 驱动恢复   ← 第一个可演示里程碑
        └─► C 状态驱动 Planner + 统一工具目录
              └─► D 审核开关 + 审核绑定版本
                    └─► E Agent 工作台前端
                          └─► F 追踪 / 计量 / 记忆 / 指标 / 评测 / 清理 / 最终验收
（F6 后端 CI 可随时提前）
```

## 与旧文档的关系

- 本目录下的 audit-report.md、current-architecture.md、target-architecture.md、data-model-design.md、migration-plan.md 是 2026-11-25 Phase 0 的产出，保留作为背景；其中与最终 Plan 冲突的地方以 [final-plan.md](final-plan.md) 为准。Phase F 的 F7 会把它们整理收敛。
- `docs/agent/`、`docs/agent-*.md`、根目录 `director_lab_full_pipeline_refactor_execution_plan.md` 为更早的历史资料，F7 会归档到 `docs/_archive/`。
- `docs/AGENTS.md` 是编码 Agent 的统一入口；在 F7 完成之前，建议先在其文档索引中加一行指向本目录。
