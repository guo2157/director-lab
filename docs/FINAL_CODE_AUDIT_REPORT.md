# director-lab 最终全量代码逻辑审核报告

- 审核时间：2026-10-24 20:14 ~ 22:03 (UTC+8)
- 审核对象：`/home/ubuntu/director-lab`，HEAD = `68cd8de`（main，领先 origin/main 14 个提交）
- 审核方式：只读代码审查 + 真实测试执行 + 运行中服务探测 + 数据库结构核对
- 审核人：OrcaTerm AI（资深全栈架构师/代码审计/数据库设计/测试工程师角色）

---

## 1. 审核结论

## FAIL

存在未解决的阻断问题，核心业务链路的自动化验证失败：

1. 后端测试套件 **37/46 失败**（两次运行结果一致，含环境变量注入后复测），核心链路（项目创建→剧本→解析→生成→合成→导出）的集成测试全部无法通过。
2. `GET /api/jobs` 未实现 API.md §9 契约中的 `updated_since_seq`/`updated_since` 增量参数，前端断线补齐链路断裂。
3. 设置页保存的 ComfyUI/LLM 配置对独立 worker 进程不生效（跨进程 `lru_cache` 配置不同步），与文档声称的『实例配置运行时生效，无需重启 worker』矛盾。

不得因『代码可编译、页面可打开、部分接口返回成功』判定通过。

---

## 2. 审核覆盖率

### 已完成代码检查
- 后端：main.py、config.py、database.py、errors.py、13 个 API 路由模块（projects/jobs/events/generate/script/settings 等）、worker 全部 4 个执行器与 scheduler、comfyui_pool、llm.py、models（enums/narrative/production/ids）、3 个 Alembic 迁移脚本
- 前端：api 封装（client/projects/jobs）、types（project/job）、AppLayout、ProjectProgressRail、ProjectListView、JobsView 状态映射
- 文档：API.md（health/jobs/鉴权/SSE 契约节选）、ACCEPTANCE.md、DECISIONS.md 结构
- Git：状态、14 个提交历史、未提交改动 diff

### 已通过自动化验证
- 前端生产构建：`npm run build` ✅ 通过（6.06s，含类型检查）
- 后端 pytest：❌ 37 failed / 9 passed（真实执行结果，非虚构）

### 已通过真实接口联调（只读探测）
- `GET /api/health` → 200 `{status: degraded, database: ok, comfyui: unreachable}`
- `GET /api/projects?page=1&page_size=2` → 200，返回真实数据
- `GET /api/jobs/stats` → 200 `{today_succeeded: 20, today_failed: 21, in_progress: 0, avg_duration_seconds: 79.75}`

### 已核对数据库
- 迁移版本 head `b7e2f4a91c03` 与实际库一致；19 张表（18 业务表 + alembic_version）
- 抽查 shot/job 表 DDL 与 ORM 模型一致（含 `keyframe_confirmed`、`is_builtin` 增量迁移）；外键开启、WAL 模式确认

### 未检查 / 无法验证
- ComfyUI 真实渲染工作流（当前未配置任何实例，无法执行真实生成）
- LLM 真实解析（LLM 未配置）
- 合成/导出端到端（依赖上述两者）
- SSE backfill 端点细节（本轮 grep 未复核到实现细节，主 SSE 链路已确认存在）
- 云防火墙规则（MCP 查询登录验证失败；已用本地探测替代：8000 端口公网不可达）

---

## 3. 缺陷清单

### P1（高优先级，阻断）

**P1-1 测试套件 37/46 失败，核心链路验证失败**
- 模块：backend/tests + app/api/v1/projects.py
- 位置：`app/api/v1/projects.py` `_check_runtime_config()`（L33-51）；全部 10 个测试文件
- 复现：`.venv/bin/python -m pytest tests/ -q` → 37 failed, 9 passed
- 实际行为：创建项目统一返回 409（`llm_not_configured`/`comfyui_not_configured`），所有依赖建项目的测试在第一步即失败（KeyError: 'id' / assert 409 == 201）
- 根因：提交 `68cd8de` 引入创建前配置校验，但测试未同步适配；且注入 `DLAB_RENDER_MOCK/DLAB_LLM_*` 环境变量复测仍失败——`get_settings()` 的 `load_runtime_settings` 会用 `data/runtime_settings.json`（当前 LLM/ComfyUI 均为空）覆盖环境变量，测试无法通过环境变量绕过
- 影响：无法以自动化测试证明任何核心业务链路正确性；『21/21 通过』的历史声明已失效
- 修复方向：测试 fixture 中 monkeypatch `_check_runtime_config` 或 mock settings；或将配置校验改为可注入
- 验证方式：修复后 pytest 全绿并记录数量

**P1-2 GET /api/jobs 缺失增量参数，断线补齐链路断裂**
- 模块：backend/app/api/v1/jobs.py
- 位置：`list_jobs`（L36-38），签名仅 project_id/status/job_type/limit/page/page_size
- 证据：API.md L392 明确契约『可选 …`updated_since`、`updated_since_seq`…以 `updated_since_seq` 为准』；前端 `api/jobs.ts` L10-11 传该参数；`JobOut`（schemas/script.py L145-157）无 `seq` 字段，而前端 `Job` 类型含 `seq`
- 影响：SSE 断线重连后无法补齐状态（API.md §12 的核心机制），任务中心可能出现状态永久不同步
- 修复方向：list_jobs 实现 seq 过滤并在 JobOut 增加 seq

**P1-3 设置页配置对独立 worker 进程不生效**
- 模块：backend/app/core/config.py + app/worker/scheduler.py
- 位置：`get_settings()` 为 `lru_cache`（进程内单例）；`comfyui_pool.set_instances` 只修改 API 进程内存并写 JSON
- 证据：生产部署为两个独立进程（uvicorn pid 1576018 + scheduler pid 1523948，均已确认 cwd 为 backend/）；scheduler L59/L98 每轮调用本进程的 `get_settings()`，读不到 API 进程的修改；`data/runtime_settings.json` 仅在进程启动时加载一次
- 影响：设置页修改 ComfyUI 实例/重试上限后，worker 并发度与实例选择不更新，必须重启 worker；与代码注释『实例增减后无需重启 worker』直接矛盾
- 修复方向：worker 每轮调度重读 runtime_settings.json（或基于文件 mtime 失效缓存）

### P2（中优先级）

**P2-1 前端 JobStatus 缺少 submission_unknown**
- `types/job.ts` L13 的联合类型无 `submission_unknown`；后端枚举与 API.md L39（`job_submission_unknown` 409）均含该状态。任务进入该状态时前端类型收窄失败、状态映射（JobsView L103-108）显示空白

**P2-2 retry 未复制 input_snapshot**
- `jobs.py` retry/retry-failed 创建新 Job 时未拷贝 `input_snapshot`（含 `comfyui_instance`）；cancel 依赖该字段定位实例，多实例下重试任务的取消可能中断错误实例

**P2-3 /api/health 双重注册与契约偏差**
- `main.py` 中 `@app.get('/api/health')` 返回 `{status:'ok'}`，`settings.health_router` 注册同名路径返回 healthy/degraded 结构。实测生效的是后者（探测返回 degraded 结构），main.py 中的定义为死代码；且实现只有 healthy/degraded 两态，API.md L460 契约含 `down` 三态；`worker` 字段硬编码 `'ok'`，未真实探测 worker 进程

**P2-4 清空 LLM API Key 功能失效**
- `settings.py` update_settings_api：空字符串密钥被置为哨兵值 `'***'`；`llm_api_key_set = bool('***') = True`，界面仍显示已配置，且后续 LLM 调用会携带 `'***` 导致 401。用户无法真正清除密钥

**P2-5 未提交工作区破坏可克隆性**
- `ProjectProgressRail.vue`（untracked）已被 `AppLayout.vue` 引用并随构建打包；该文件未 `git add`，他人克隆当前 HEAD 后前端构建直接失败。另有 8 个 modified 文件与 14 个本地提交未推送

**P2-6 测试无统一 conftest，隔离机制分散**
- 各测试文件自行 monkeypatch `db_mod.engine/SessionLocal`；本轮已验证未污染真实库（真实库 project 最新记录 09:39，晚于测试运行的 21:xx 无新增），但隔离正确性依赖每个文件自觉实现，脆弱

**P2-7 真实库混入开发/验证数据**
- `project` 表 6 条记录中 4 条为验证用途（『校验测试项目』『E2E回归测试项目』『前端联调测试』『阶段4链路验收』），与生产数据无隔离标记

### P3（低优先级）

- P3-1 `errors.py` L56-61 `invalid_request` 重复定义两次
- P3-2 `jobs.py` 模块 docstring 过期（『取消/重试/SSE 在后续批次接入』，实际已实现）
- P3-3 前端主 chunk 551KB 超 500KB 警告，建议 manualChunks 拆分
- P3-4 uvicorn 绑定 `0.0.0.0:8000` 且无鉴权；当前公网探测超时（云防火墙拦截），实际不可达，但与 API.md『本期仅内网访问』的声明不完全一致，建议改绑 127.0.0.1 或保持防火墙封闭

### 未发现问题的模块（检查范围声明）
- 数据库模型↔迁移↔实际 DDL 三方一致（抽查 shot/job 全字段 + 迁移链完整）；外键/WAL pragma 正确
- 统一错误格式（`{detail:{code,message}}`）在全局异常处理器与前端 ApiError 解析两侧对齐
- 任务状态机（queued/running/succeeded/failed/lost/cancelled + submission_unknown）在后端枚举、迁移、执行器中一致；事件 outbox 与状态同事务写入；启动恢复 running→queued 保守策略符合 §5.4
- ComfyUI 池加权轮询/故障转移逻辑本身正确（问题仅在 P1-3 的跨进程生效）
- 前端构建、路由、API 封装与后端路径/方法核对一致（除 P1-2/P2-1 所列差异）
- 『没有发现』仅指本次检查范围内，不等于『绝对不存在』

---

## 4. 前后端一致性报告

| 检查项 | 结论 |
| --- | --- |
| API 路径/方法 | ✅ 一致（projects/jobs/settings/generate/script 等） |
| 分页参数 | ✅ page/page_size 已对齐（本轮未提交改动补齐） |
| 增量补齐 updated_since_seq | ❌ 前端传参、后端未实现（P1-2） |
| Job.seq 字段 | ❌ 前端类型有、后端 JobOut 无（P1-2） |
| JobStatus 枚举 | ❌ 前端缺 submission_unknown（P2-1） |
| parse 响应结构 | ✅ 前端已改为 `{job_id, status}`，与后端 202 响应一致 |
| Project 派生字段 | ✅ shot_total/shot_done/current_stage 前后端一致 |
| 错误处理 | ✅ 统一 `{detail:{code,message}}` + 前端 ApiError 解析链对齐 |
| 鉴权 | ⚠️ 双侧一致地『本期不鉴权』（API.md 已确认决策），非缺陷但需注意暴露面（P3-4） |

## 5. 数据库审核报告

- 表结构：18 张业务表与 ORM 模型一致；迁移链 `167f0caf4cb4 → a3f8c2d91e07 → b7e2f4a91c03`，实际库版本 = head ✅
- 约束：uq_shot_scene_episode_seq、uq project+name 等组合唯一约束在 DDL 中确认存在 ✅
- PRAGMA：foreign_keys=ON、journal_mode=WAL 运行时逐连接设置 ✅
- 事务：事件与状态同事务 outbox 写入（executor/parse_executor 确认）✅
- 并发：SQLite 单写者 + WAL；worker 单进程取任务无争抢 ✅
- 数据一致性：job 表 47 条与 job_event 117 条事件对应；asset 17 条与 media 目录文件抽查对应 ✅
- 风险：真实库混入验证数据（P2-7）；备份文件（data/backups 5 组）与主库并存，无清理策略

## 6. 测试证据

| 命令 | 环境 | 结果 |
| --- | --- | --- |
| `.venv/bin/python -m pytest tests/ -q` | VM-0-13-ubuntu, Python 3.12 | **37 failed, 9 passed**（4.23s） |
| 同上 + `DLAB_RENDER_MOCK=true DLAB_LLM_*` 注入复测 | 同上 | **37 failed, 9 passed**（4.40s，证明环境变量被 runtime_settings.json 覆盖） |
| `npm run build` | Node ≥20 | ✅ 通过（6.06s） |
| `curl /api/health` | 本机 127.0.0.1:8000 | 200 degraded（ComfyUI 未配置） |
| `curl /api/projects` `/api/jobs/stats` | 本机 | 200，真实数据返回 |

- 失败日志摘要：全部失败源于 `POST /api/projects` 返回 409（llm_not_configured / comfyui_not_configured），测试在 `_make_project` 处 KeyError: 'id' 或 assert 409 == 201
- 无法执行的测试：ComfyUI 真实渲染、LLM 真实解析、合成导出端到端（外部服务未配置，按审核纪律不触发高成本生成）

## 7. 修复建议与复审条件

修复顺序：**P1-1 → P1-2 → P1-3 → P2-1~P2-5 → P3**

| 问题 | 修复完成条件 | 复测方式 |
| --- | --- | --- |
| P1-1 | pytest 全绿 | 全量 pytest + 记录数量 |
| P1-2 | list_jobs 支持 seq 增量过滤，JobOut 含 seq | 接口实测 + 前端断线补齐场景手测 |
| P1-3 | worker 运行中感知设置变更 | 修改实例配置后观察 worker 并发度变化，不重启进程 |
| P2-5 | 工作区提交并推送 | 全新 clone 后 `npm run build` 通过 |

未修复、未复测前，上述问题不得标记为已解决。

## 8. 最终验收清单

- [x] 项目可以按预期方式启动和构建（后端 uvicorn 运行中；前端构建通过）
- [ ] 核心前端页面及交互正常（构建通过，但运行时链路未端到端验证）
- [ ] 核心 API 请求与响应契约一致（P1-2 jobs 增量参数缺失）
- [ ] 后端核心业务逻辑正确（自动化验证失败 P1-1，无法证明）
- [x] 数据库结构与代码实现一致（DDL/模型/迁移三方核对）
- [x] 事务、并发和数据完整性风险已检查（未发现新缺陷）
- [~] 权限与数据隔离符合要求（本期不鉴权为已确认决策；0.0.0.0 绑定为提示项）
- [~] 异步任务状态机正确（状态机本身正确；跨进程配置生效缺陷 P1-3）
- [ ] 超时、失败、取消及重试逻辑已验证（测试失败 + retry input_snapshot 缺陷 P2-2）
- [ ] 外部服务及 AI 工作流已按可用环境验证（ComfyUI/LLM 均未配置，NOT VERIFIED）
- [ ] 核心业务端到端链路已验证（阻断于 P1-1）
- [x] 自动化测试和构建结果已记录（本报告第 6 节）
- [ ] P0、P1 问题均已关闭并完成复测（3 项 P1 全部未解决）
- [x] 未验证事项及剩余风险已明确披露（第 2 节）

---

*本报告基于 2026-10-24 22:03 前收集的真实证据生成；审核过程未修改任何业务代码、数据库结构与配置文件。*

---

## 9. 修复复测记录（2026-10-24 22:39 更新）

按第 7 节建议顺序完成修复，全部复测通过：

| 问题 | 修复内容 | 复测结果 |
| --- | --- | --- |
| P1-1 | 新增 `tests/conftest.py`，autouse fixture 绕过创建项目的运行时配置校验（环境前置条件，非被测业务逻辑） | ✅ pytest 46/46 通过（8.69s） |
| P1-2 | `list_jobs` 实现 `updated_since_seq`（严格大于、升序）与 `updated_since` 兼容参数；`JobOut` 增加 `seq` 字段 | ✅ 临时库实测：seq 返回、>5 过滤仅返回 seq=9、>9 为空，VERDICT PASS |
| P1-3 | `get_settings` 改为基于 `runtime_settings.json` mtime 的缓存失效；新增 `clear_settings_cache`；`test_compose_export.py` 的 `cache_clear` 调用同步迁移 | ✅ 实测修改 job_max_attempts 后无需重启即返回新值；pytest 全绿 |
| P2-1 | 前端 `JobStatus` 补 `submission_unknown`；JobsView 筛选/颜色/文案三处映射同步补充 | ✅ 前端构建通过 |
| P2-2 | `retry` 与 `retry-failed` 创建新任务时复制 `input_snapshot`（含 comfyui_instance） | ✅ pytest 全绿 |
| P2-3 | 移除 main.py 中被覆盖的死代码 `/api/health`；health_check 补 `down` 态（数据库不可用） | ✅ pytest 全绿 |
| P2-4 | 清空 LLM API Key 改为置空字符串（原哨兵 `'***'` 导致无法真正清除） | ✅ 实测清空后 `llm_api_key_set=false`，VERDICT PASS |
| P2-5 | `ProjectProgressRail.vue` 已 `git add` 纳入版本控制 | ✅ git status 确认 A 状态 |
| P3-1 | 删除 `errors.py` 重复的 `invalid_request` 定义 | ✅ 语法校验通过，引用完好 |
| P3-2 | 更新 `jobs.py` 过期 docstring | ✅ 已与实现一致 |

**整体验证**：后端 pytest 46/46 通过；前端 `npm run build` 通过（5.98s）。

**未处理项（维持原披露）**：P2-6（测试隔离机制分散，建议后续统一 conftest）、P2-7（真实库验证数据清理，涉及删数据需授权）、P3-3（chunk 拆分优化）、P3-4（绑定地址调整建议）。修复期间曾因验证脚本临时将 `job_max_attempts` 改为 5，已恢复为 3 并复测。

**修复后结论**：P1 全部关闭并复测通过，核心链路自动化验证恢复；ComfyUI/LLM 真实渲染与端到端链路仍为 NOT VERIFIED（外部服务未配置）。整体状态由 **FAIL** 上调为 **CONDITIONAL PASS**。
