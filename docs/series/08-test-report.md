# 08 测试报告

> 所有数据来自实际执行，未做估算。

## 1. 测试环境

| 项 | 值 |
|----|-----|
| 执行时间 | 2026-11-10 |
| 运行目录 | `/home/ubuntu/director-lab/backend` |
| 解释器 | `backend/.venv/bin/python` |
| 数据库 | SQLite（WAL） |
| 迁移版本 | `a1f4c8e2d907 (head)` |
| 单测框架 | pytest（同步 TestClient） |

## 2. 全量回归实测

```
25 failed, 614 passed, 65 warnings in 136.02s (0:02:16)
```

### 2.1 与基线对比

| 时点 | failed | passed | 说明 |
|------|--------|--------|------|
| 本次改动前（git worktree 基线复跑） | 25 | 592 | 既有基线问题 |
| 本次改动后 | 25 | 614 | 新增 22 条系列测试全部通过 |

**结论**：失败数未增加（25 → 25），通过数增加 22，与新增的 `tests/test_series_management.py` 用例数一致。本次改动未引入新的失败。

## 3. 失败用例分布（均为既有基线问题）

| 测试文件 | 失败数 |
|----------|--------|
| `tests/test_agent_generate_approval.py` | 10 |
| `tests/test_agent_e2e_flow.py` | 6 |
| `tests/test_generate_task_integration.py` | 4 |
| `tests/test_agent_tasks.py` | 2 |
| `tests/test_task_dependency_gate.py` | 1 |
| `tests/test_prompt_docs_sync.py` | 1 |
| `tests/test_prompt_compiler.py` | 1 |
| **合计** | **25** |

已确认的样例：

- `test_task_dependency_gate.py::test_stage_blocked_when_dependency_failed`
- `test_prompt_docs_sync.py::test_doc_template_matches_runtime[compile_shot_video.md-...]`（文档模板与运行时提示词不一致）

这些失败集中在**既有 Agent 审批 / 生成任务 / 提示词文档同步**领域，与系列化模块无交集：系列模块不导入也不修改 `agent_*`、`task_dependency_gate`、`prompt_compiler`、`prompt_docs` 相关代码。

## 4. 系列模块专项测试

```
22 passed in 7.08s
```

文件：`tests/test_series_management.py`

覆盖范围（按接口维度）：

| 分组 | 覆盖点 |
|------|--------|
| 系列主档 | 创建、列表（关键词/状态/分页）、详情、更新、归档、恢复、归档后禁止续写 |
| Bible | 读取激活版本、保存为新版本、版本历史、无激活版本报错 |
| 剧集 | 创建（自动集号/指定集号）、列表分页、详情、下一集号、工作区 |
| 上下文 | 预览（不落库）、清单查询、无记录报错 |
| 剧本 | 保存草稿（新建版本）、列表、确认、重新生成、已确认版本拒绝重复生成 |
| 连续性 | 概览、快照历史、执行检查、问题列表（未检查态） |
| 剧情线 | 列表、创建、更新、状态流转 |
| 资产 | 登记（不复制）、系列资产列表、某集资产、使用历史、名称解析候选 |

## 5. 编译与静态检查

| 检查项 | 命令 | 结果 |
|--------|------|------|
| 后端语法 | `py_compile` 全部 series 相关文件 | `PY_COMPILE_OK` |
| 迁移 head | `alembic heads` | `a1f4c8e2d907 (head)`，单 head |
| 前端类型 | `npx vue-tsc --noEmit -p tsconfig.json` | `EXIT=0`，日志 0 行 |
| OpenAPI 路径 | `len(app.openapi()['paths'])` | 183 |
| 系列相关路径 | 过滤统计 | 30（含 3 条既有 audio/scenes 匹配项，系列新增 27） |

## 6. 接口挂载验证

使用 `app.openapi()['paths']` 统计（不使用 `app.routes`，因为 include_router 在 FastAPI 中表现为 `_IncludedRouter`，直接遍历会漏计）：

- 总路径：183
- 既有接口全部存在，未出现路径丢失
- 系列 27 条路径全部注册成功

## 7. 前端构建验证

```bash
cd frontend && npx vue-tsc --noEmit -p tsconfig.json
# EXIT=0，输出 0 行
```

新增/修改的前端文件均通过类型检查：

| 文件 | 行数 |
|------|------|
| `views/series/SeriesListView.vue` | 260 |
| `views/series/SeriesCreateView.vue` | 153 |
| `views/series/SeriesDetailView.vue` | 496 |
| `views/series/SeriesContinueView.vue` | 388 |
| `views/series/EpisodeReviewView.vue` | 482 |
| `api/series.ts` | 201 |
| `types/series.ts` | 314 |
| `api/tasks.ts` | 60（原 43，新增 `TaskDetail` + `getTask`） |
| `router/index.ts` | 111（原 81，新增 5 条路由） |
| `components/AppLayout.vue` | 302（原 289，新增系列入口与白名单判定） |

## 8. 兼容性验证

| 验证目标 | 方法 | 结论 |
|----------|------|------|
| 既有接口未破坏 | OpenAPI 183 条路径与改动前一致 | 通过 |
| 单集生产链路未破坏 | 既有测试通过数从 592 → 614，无新增失败 | 通过 |
| 既有前端路由未改动 | `router/index.ts` 原 10 条路径逐一核对，路径与 name 均未变 | 通过 |
| 项目页布局未受影响 | `isProjectPage` 改白名单后，项目级路由仍命中 | 通过 |
| 数据未被删除 | 迁移仅新增表，无 drop/alter 既有表 | 通过 |

## 9. 未验证项（如实说明）

1. **端到端 UI 手工验证**：本次仅完成类型检查与后端接口测试，未在浏览器中逐页点击验证 5 个新页面的真实交互与视觉表现。
2. **真实 LLM 调用**：`series_agent` 执行链路依赖 `LLMClient`，测试中未跑通真实模型生成，剧本生成阶段的成功路径未经真实模型验证。
3. **并发确认压测**：乐观锁与幂等键的逻辑有单元覆盖，但未做并发压力测试。
4. **既有 25 个失败用例**：属基线问题，本次未修复，也未纳入本任务范围。
5. **历史剧本修改影响分析**：该能力未实现，测试不覆盖。

## 10. 结论

- 系列化模块（后端 27 条接口 + 11 张表 + 6 个服务 + 1 个 Worker + 前端 5 个页面）功能链路完整，专项测试 22/22 通过。
- 全量回归失败数未增加，确认既有失败为基线问题。
- 前后端均通过编译/类型检查，旧链路未被破坏。
- 尚未做浏览器端 E2E 与真实模型调用验证，上线前建议补一次冒烟。
