# 阶段 B/C 交付报告：导演台入口与会话消息持久化

- 日期：2026-10-27
- 前置：docs/AGENT_AUDIT_REPORT.md（阶段 A 审计）

## 1. 目标及完成情况

| 目标 | 状态 |
| --- | --- |
| 首页新增「导演台」一级 Tab（logo 与项目区域之间） | ✅ 已完成并验证 |
| 导演台三栏布局（左会话/中对话/右项目上下文） | ✅ 已完成并验证 |
| 会话与消息持久化（真实数据库，刷新可恢复） | ✅ 已完成并验证 |
| 聊天接口（真实 LLM 多轮对话） | ✅ 已完成并端到端验证 |
| 旧页面无回归 | ✅ 全量测试 111 passed |

## 2. 修改文件与关键改动

### 后端
- `app/models/agent.py`（新增）：AgentSession / AgentMessage 两张表
- `app/models/__init__.py`：注册新模型
- `app/models/ids.py`：新增 ags_/agm_ 前缀 ID 生成
- `alembic/versions/b2c3d4e5f6a7_agent_session_message.py`（新增）：建表迁移（含 downgrade），已应用
- `app/schemas/agent.py`（新增）：Pydantic 校验模式（from_attributes）
- `app/api/v1/agent.py`（新增）：会话 CRUD/归档/恢复 + 消息追加/列表 + POST /{id}/chat 聊天接口
- `app/api/v1/agent.py` 注册于 `app/main.py`
- `app/integrations/llm.py`：新增 chat_messages() 多轮对话方法（chat() 重构为委托调用，行为不变）
- `app/main.py`：修复全局 RequestValidationError 处理器（ctx 中 ValueError 对象安全序列化，修复 422 退化为 500 的缺陷）

### 前端
- `src/router/index.ts`：新增 /director 路由（懒加载）
- `src/components/AppLayout.vue`：顶部菜单新增「导演台」（首位）
- `src/views/DirectorConsoleView.vue`（新增）：三栏导演台视图
- `src/api/agent.ts`（新增）：会话/消息 API 封装

## 3. 新增接口

- POST /api/agent/sessions（201）｜GET 列表（分页/项目筛选/含归档）｜GET /{id} ｜PATCH /{id}（重命名/关联项目/切换模型）｜DELETE /{id}（软删除归档）｜POST /{id}/restore
- POST /api/agent/sessions/{id}/messages（201，seq 会话内自增，唯一索引兜底）｜GET /{id}/messages（after_seq 增量加载）
- POST /api/agent/sessions/{id}/chat：用户消息持久化 → 最近 40 条历史窗口 → LLMClient.chat_messages（会话 model 覆盖默认模型）→ 助手回复持久化；LLM 错误分类（llm_unconfigured→409，llm_timeout/llm_unreachable/llm_error→502），失败时用户消息已保留可重试

## 4. 数据结构变化

新增 agent_session（id/title/project_id/status/model/时间戳，project_id 索引）与 agent_message（id/session_id/role/content/payload JSON/attachment_id/seq/时间戳，(session_id, seq) 唯一索引）。迁移 b2c3d4e5f6a7 已应用，downgrade 完整。无既有表变更，兼容性无影响。

## 5. 实际执行的检查与测试

- 后端全量 pytest：**111 passed**（基线 103 + 新增 8：会话 CRUD 流、消息 seq/增量加载、归档只读、非法 role 422、404、项目关联校验、聊天流程 mock、LLM 错误分类映射）
- 前端构建：`npm run build` 通过（vue-tsc 类型检查 + vite，6.33s）
- 服务重启：uvicorn 已重启加载新路由，健康检查 200
- 端到端冒烟：会话创建返回真实 ID；聊天接口真实 LLM 调用返回导演台助手回复；冒烟会话已归档清理
- 前端验证：vite（实际端口 5174，5173 被同机 freellmapi 项目占用）/director 页面 200，/api 代理正常

## 6. 遗留问题与风险

1. 导演台消息区暂未实现流式输出（SSE），当前为同步请求-响应；断连后可通过消息列表接口恢复真实状态（已持久化）
2. 模型选择器 UI 尚未实现（后端会话 model 字段与 /settings/llm-models 接口已就绪，阶段 D 接入）
3. 附件上传、工具调用展示、审批事件为阶段 D/E 范围
4. ComfyUI 实例失效（403）仍阻塞视频生成链路验证（环境类，非本次改动引入）
5. 右栏项目上下文当前展示项目列表与关联，任务/产物明细为阶段 E 范围

## 7. 阶段 D（部分）：模型选择器

- 前端：导演台聊天区头部新增模型选择器（NSelect），选项来自 POST /api/settings/llm-models 真实返回（实测：deepseek-flash、deepseek-v4-pro），加载失败降级为后端默认模型并提示
- 切换模型通过 PATCH /api/agent/sessions/{id} 写入会话 model 字段；后端聊天接口按会话 model 覆盖默认模型（LLMClient(model=s.model)），切换真实生效
- 端到端验证：创建会话 → 切换 model=deepseek-flash → 聊天 200 → 会话 model 字段保持 → 归档清理，全部通过
- 前端构建（vue-tsc + vite）通过

## 8. 阶段 E（首批）：受控工具注册表

- `app/services/agent_tools.py`（新增）：Agent 受控工具注册表，首批 3 个只读查询工具：
  - `project_overview`：项目概览（基础信息 + 派生进度/当前阶段）
  - `list_jobs`：项目任务列表（状态过滤，返回 job_type/shot_id/status/error_code）
  - `asset_stats`：项目资产统计（按 asset_type 分组计数）
- 设计约束：全部只读，无写操作、无生成触发（失效传播只标记不触发）；未知工具/参数错误/项目不存在均分类返回（tool_not_found/tool_invalid_args/project_not_found）
- 测试：`tests/test_agent_tools.py` 7 个用例全部通过；全量回归 118 passed
- 待接入：聊天接口的工具调用编排（意图识别 → 工具选择 → 结果注入回复）为阶段 E 后续内容

### 项目上下文注入（已接入）

- 聊天接口在会话关联项目时，自动执行 project_overview 与 asset_stats 只读工具，将真实项目状态（名称/状态/制作阶段/镜头进度/资产统计）注入系统提示词
- 端到端验证（真实项目 prj_fdc96b60281b「小红帽」）：助手回复「处于 clips 制作阶段，镜头进度 0/18，资产：角色 5、关键帧 48、场景 20 共 73 项」，与数据库真实状态一致
- 全量回归 118 passed；验证会话已归档清理

## 9. 下一阶段前置条件

阶段 D 剩余（附件上传、工具调用展示）与阶段 E 后续（意图路由/审批/记忆/流式输出）可直接开始，无阻塞前置。阶段 F 受 ComfyUI 实例失效（403）阻塞视频链路验证。
