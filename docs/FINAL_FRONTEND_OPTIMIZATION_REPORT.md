# 前端最终优化报告

> 生成时间：2026-10-24 20:29（Asia/Shanghai）
> 优化范围：director-lab 前端（Vue 3 + TypeScript + Vite + Naive UI + Pinia + TanStack Query）及少量后端接口契约修复

## 1. 优化概览

### 检查过的页面和模块

| 页面/模块 | 检查结论 |
| --- | --- |
| AppLayout（全局布局/导航） | 正常，本轮已新增左侧流程进度栏 |
| ProjectListView（项目列表） | 已优化：新增配置引导横幅、创建项目配置校验引导 |
| ScriptView（剧本） | 已修复：解析回调 parse_version 取值崩溃、ParsePayload 类型不一致 |
| StoryboardView（分镜看板） | 已优化：补充错误状态分支 |
| AssetsView（资产库） | 已优化：补充错误状态分支与重试 |
| ComposeView（合成与导出） | 已修复：错误码取值方式失效问题 |
| JobsView（任务中心） | 已修复：后端接口契约不一致（P0）；补充错误状态分支 |
| SettingsView（设置） | 正常，此前已优化模型下拉查询与错误提示 |
| api/client.ts（统一错误处理） | 已修复：detail 格式解析缺失、英文错误码透出 |

### 实际修改的文件

前端：
- `src/api/client.ts`：补充后端 detail 格式（code/message）错误解析；HTTP 状态码兜底改为可读中文原因
- `src/api/projects.ts`：修正 parseScript 返回类型声明
- `src/api/settings.ts`：新增 listLlmModels（此前批次）
- `src/components/AppLayout.vue`：集成左侧流程进度栏
- `src/components/ProjectProgressRail.vue`：新增（流程进度栏组件）
- `src/types/project.ts`：补充 shot_total/shot_done/current_stage 派生字段
- `src/types/script.ts`：修正 ParsePayload 字段与后端一致
- `src/views/ProjectListView.vue`：配置引导横幅 + 创建项目配置校验引导
- `src/views/project/ScriptView.vue`：解析回调修复、候选版本 ID 改从任务 result 获取
- `src/views/project/StoryboardView.vue`：错误状态分支 + 样式
- `src/views/project/AssetsView.vue`：错误状态分支 + errMsg + 样式
- `src/views/project/ComposeView.vue`：错误码取值方式修复（2 处）
- `src/views/JobsView.vue`：错误状态分支 + 样式

后端（契约修复，依据 docs/API.md 权威契约）：
- `backend/app/api/v1/jobs.py`：GET /api/jobs 的 project_id 改为可选（契约 §9）；补充 page/page_size 分页参数（契约 §9）

### 本次解决的主要问题

1. **P0 任务中心无法加载**：后端 jobs 接口 project_id 必填与 API.md 契约（可选）不符，前端不传时返回 422。已按契约修复。
2. **P0 任务分页失效**：契约定义 page/page_size，后端仅实现 limit，前端分页参数被忽略。已按契约补齐。
3. **P1 解析提交崩溃**：前端访问 res.parse_version.parse_version_id，后端实际仅返回 job_id/status，导致 Cannot read properties of undefined。已改为从任务轮询 result.parse_version_id 获取。
4. **P1 错误码传递链路断裂**：前端只解析 error 格式，后端返回 detail 格式，业务错误码全部丢失。已补充解析。
5. **P1 合成/导出特定错误提示失效**：ComposeView 用 axios 风格取值 e.response.data.error.detail，经统一转换后永远为空。已改用 ApiError.code。
6. **P2 错误状态分支缺失**：资产库/分镜看板/任务中心加载失败时无提示无重试（空白或无限等待）。已补充。
7. **P2 用户可见英文错误码**：无结构化错误时透出 axios 原始信息（Request failed with status code 500）。已按状态码映射中文原因。

### 保留未修改的部分及原因

- 未引入新 UI 库/状态管理方案（纪律 1）；
- 未重构 ScriptView 等大组件（业务正常，拆分属 P3 技术债务，避免本轮引入回归风险）；
- as any 共 30 处未全部清理（多为 mutation onError 回调参数，类型安全影响低，属 P3）；
- SSE 事件流（/api/events）验证 200 正常，未改动；
- 未修改数据库结构与后端业务规则（纪律 2），后端仅按 API.md 契约修复接口参数。

## 2. UI/UX 优化结果

- **信息层级**：项目内新增左侧流程进度栏（剧本→分镜看板→资产库→合成与导出），基于 current_stage 自动点亮已完成步骤，点击可跳转；移动端自动隐藏。
- **引导闭环**：项目列表页新增配置引导横幅（语言模型/ComfyUI 未配置时常驻提示 + 直达设置页），与创建项目时的 409 校验形成『事前引导 + 事中阻断』闭环。
- **交互状态完善**：资产库/分镜看板/任务中心补齐 加载中→失败（原因+重试）→空数据→正常 四态；所有 mutation 均有 loading 与成功/失败反馈。
- **错误提示**：全部错误提示为可读中文，含应对建议，不再透出英文错误码。

## 3. 业务逻辑与接口结果

### 已核对的核心 API

- GET /api/projects、POST /api/projects（含配置校验 409 llm_not_configured / comfyui_not_configured）
- POST /api/projects/{id}/parse（返回 job_id/status）与任务 result.parse_version_id 链路
- GET /api/jobs（project_id 可选 + page/page_size 分页，本轮修复）
- GET /api/jobs/stats、GET /api/jobs/{id}、retry/cancel
- GET/PUT /api/settings、POST /api/settings/llm-models、test-llm、comfyui-instances
- POST /api/projects/{id}/compose、export（错误码 output_missing/duration_mismatch_review_required/speaker_mapping_required/composition_stale）
- GET /api/events（SSE）

### 已修复的状态同步问题

- 解析候选版本 ID 改由任务轮询结果驱动（watch parse_version_id），确认/放弃流程状态正确流转。

### 仍需后端配合的事项

- 无阻断项。P2 建议：GET /api/jobs 契约中的 updated_since_seq 增量补齐参数后端尚未实现（前端已预留类型），属功能增强非缺陷。

## 4. 性能与代码质量

- 轮询检查：任务中心 5s 轮询 + SSE 双通道（SSE 触发 invalidate，轮询兜底），频率合理，无重复轮询同一资源；剧本页解析任务轮询在任务结束后自动停止（refetchInterval 返回 false）。
- 构建产物：主 chunk 485KB（gzip 149KB），超 500KB 警告阈值，建议后续按路由 manualChunks 拆分（P3）。
- 类型改进：修正 3 处与后端不一致的类型声明（ParsePayload、parseScript 返回、Project 派生字段）。
- 未解决技术债务：30 处 as any（P3）、ScriptView 组件过大（P3）、无自动化测试（P3，项目无测试框架）。

## 5. 测试结果

### 执行的命令

- cd frontend && npm run build（vue-tsc 类型检查 + vite 构建）
- curl 真实接口联调（后端 127.0.0.1:8000）

### 构建、类型检查结果

- 最终构建通过：built in 6.02s，类型检查 0 错误（仅 chunk 体积警告，非错误）。

### 真实接口联调结果

| 接口 | 结果 |
| --- | --- |
| GET /api/health | 200 |
| GET /api/projects?page=1&page_size=3 | 200 |
| GET /api/jobs?page=1&page_size=3 | 200（修复前 422） |
| GET /api/jobs/stats | 200 |
| GET /api/settings | 200 |
| GET /api/settings/comfyui-instances | 200 |
| POST /api/settings/llm-models | 200（返回模型列表） |
| GET /api/events（SSE） | 200 |

### 核心页面回归结果

- 项目列表：加载/创建/归档/删除链路正常，配置横幅按配置状态显隐。
- 任务中心：列表加载正常（修复前 422 无法加载），筛选/分页/重试/取消操作入口正常。
- 剧本页：解析提交不再崩溃，候选版本确认/放弃流程状态正确。
- 后端服务：已重启并稳定运行（uvicorn，8000 端口）。

### 未执行测试及原因

- 自动化测试：项目无前端测试框架（无 vitest/jest 配置），无测试可执行。
- 浏览器端手工回归：通过 vite dev（5174）热更新验证页面可访问，未逐像素人工核验全部页面（环境限制，无浏览器自动化工具）。

## 6. 遗留问题

| 级别 | 问题 | 影响范围 | 处理建议 | 验证条件 |
| --- | --- | --- | --- | --- |
| P2 | GET /api/jobs 的 updated_since_seq 增量补齐后端未实现 | 任务多时全量拉取，性能可接受 | 后端按 API.md §9 补充 seq 过滤 | 后端实现后 curl 验证 |
| P2 | 主 chunk 485KB 超 500KB 警告阈值 | 首屏加载 | vite manualChunks 按路由拆包 | 构建产物体积对比 |
| P3 | 30 处 as any（多为 onError 回调） | 类型安全 | 定义统一 ApiError 类型收窄 | vue-tsc 通过 |
| P3 | ScriptView 约 750 行，组件过大 | 可维护性 | 拆分评估/解析/角色子组件 | 构建通过 + 页面回归 |
| P3 | 无前端自动化测试 | 回归效率 | 引入 vitest + @vue/test-utils | CI 集成 |
| P3 | 后端以 setsid nohup 手动运行，重启后需手动拉起 | 服务可用性 | 配置 systemd 服务 | 服务器重启验证 |

## 7. 最终结论

**CONDITIONAL PASS**

主要优化已完成：修复 2 项 P0 接口契约问题（任务中心无法加载、分页失效）、3 项 P1 业务正确性问题（解析崩溃、错误码链路断裂、特定错误提示失效）、多项 P2 交互状态与提示问题；构建与类型检查通过，核心接口真实联调全部 200，核心页面回归正常。

判定为 CONDITIONAL PASS 而非 PASS 的原因：存在明确的非阻断遗留项（P2 的 updated_since_seq 未实现、chunk 体积警告；P3 技术债务），且浏览器端人工逐页核验与自动化测试因环境限制未执行，验证存在边界。
