# Director-Lab 联网搜索能力审计报告（Phase 0 资产盘点）

- 日期：2026-11-04
- 审计范围：后端搜索链路 / 工具注册 / LLM 协议 / 提示词 / 前端事件 / 日志表
- 结论先行：**当前系统没有任何可被 Agent 调用的搜索工具**；唯一搜索能力是 video_workflow 内部 httpx 页面抓取（非工具、无日志、无状态机、无前端展示）

## 一、能力矩阵

| # | 功能 | 代码位置 | 当前实现 | 状态 | 处理方案 |
|---|---|---|---|---|---|
| 1 | search_web_materials | app/services/video_workflow.py:164 | 百科优先（baike/wikipedia，均 403 被拦）→ 降级 bing/360 搜索结果页抓取；正则去标签提取；今日已修复并实测可用（小马过河 5917 字符） | **PARTIAL** | 抽取为可复用 Provider，不重复实现 |
| 2 | HTTPX 请求封装 | video_workflow 内联 httpx.Client | 每次调用新建 Client，无共享封装、无统一超时/重试/UA 配置 | **PARTIAL** | 新建 web_fetch.py 统一封装（SSRF 防护/大小限制/超时），video_workflow 后续复用 |
| 3 | Bing 搜索 Provider | video_workflow.search_web_materials 内部 | 仅作为降级抓取分支硬编码，非独立 Provider，无结构化结果（title/url/snippet） | **PARTIAL** | 抽取为 BingProvider（结果页解析出结构化条目） |
| 4 | 360 搜索 Provider | 同上 | 同上 | **PARTIAL** | 抽取为 SoProvider |
| 5 | HTML 解析/正文提取 | video_workflow 正则（去 script/style/标签 + 结果区标记截取） | 能用但无结构化（无 title/url/snippet 拆分），导航噪音靠标记截取 | **PARTIAL** | Provider 内实现结果条目解析（标题/链接/摘要） |
| 6 | TOOL_REGISTRY 注册机制 | app/services/agent_tools.py:93 | 机制完备：25 个工具，Schema 校验 + 审批门禁 + ToolResult 结构化返回 + list_tools 序列化给 LLM；**无任何搜索工具** | **EXISTING（机制）/ MISSING（搜索工具）** | 新增 web_search / web_fetch 工具注册 |
| 7 | LLM 工具调用协议 | app/integrations/llm.py + agent.py chat 端点 | LLM 无原生 function-calling 循环；工具清单序列化进提示词，由确定性代码（意图路由/审批流）执行工具——符合 P0 原则（模型只识别，代码执行） | **EXISTING** | 沿用现有协议：意图路由触发 web.search 意图 → 确定性代码执行搜索工具 |
| 8 | 搜索相关系统提示词 | prompt_store（外置 workspace/prompts/*.md） | 无任何搜索规则提示词（不伪装搜索/不编造结果/保留来源 URL 均未约定） | **MISSING** | 主对话系统提示词追加搜索规则段 |
| 9 | 前端 SSE/WebSocket 事件 | 后端 events.py（job 域 SSE 已有）；前端 DirectorConsoleView.vue | 后端 SSE 仅覆盖 job 事件；前端聊天为同步请求/响应 + payload 渲染（asset_refs 审批卡片已有先例），**不消费 SSE** | **PARTIAL** | 一期：搜索执行记录随 assistant payload 返回（复用 asset_refs 卡片模式）；SSE 搜索事件流为 job 域二期 |
| 10 | Alembic 日志/任务表 | alembic/versions（job/job_event/agent_session 等 18 表） | job 域日志完备；**无 search_run/search_result 表**，搜索无任何持久化记录 | **MISSING** | 新建 SearchRun/SearchResult 表 + 迁移 |

## 二、关键结论

1. **Agent 无法搜索的根因**：TOOL_REGISTRY 25 个工具中无搜索工具；LLM 提示词中无搜索规则；用户要求搜索时 Agent 只能靠内部知识回答或拒绝——这正是『伪装搜索/编造结果』风险的来源。
2. **唯一真实搜索能力**是 video_workflow 的页面抓取（今日已修复 403 降级链路并实测可用），但它是工作流内部实现：无 search_run_id、无状态机、无日志表、无前端事件、异常被 except 吞掉只返回文案。
3. **可复用资产充足**：工具注册/Schema 校验/审批门禁/ToolResult、payload 卡片渲染先例（asset_refs）、job 域 SSE、Alembic 迁移规范——搜索系统应全部复用，不重复建设。

## 三、实施顺序（先修 BROKEN → 补 PARTIAL → 建 MISSING）

- 无 BROKEN 项（今日已修复 403 降级与提示词注册两个缺陷）。
- **Phase 1 工具层**：新建 app/services/web_search.py（web_search 工具 + Bing/So Provider Adapter，复用今日验证过的抓取链路，零 API Key）+ app/services/web_fetch.py（SSRF 防护/大小限制/超时/编码处理）。
- **Phase 2 状态机与日志**：app/models/search.py（SearchRun/SearchResult）+ Alembic 迁移 + 搜索状态机（pending→searching→validating→fetching→summarizing→success/partial/empty/failed/cancelled）+ search_run_id + Search Result Validator + 分类重试（超时退避/空结果换关键词一次/Provider 故障切换）。
- **Phase 3 集成**：intent_router 接入 web.search 意图 → chat 端点确定性执行搜索 → 系统提示词搜索规则（15 条）→ assistant payload 携带搜索执行记录 → 前端 DirectorConsoleView 搜索卡片（复用 asset_refs 模式）→ 测试。

## 四、设计决策

- 同步执行 + payload 事件（SSE 为 job 域，二期再做）。
- 搜索与网页读取分离（web_search 发现 URL / web_fetch 取正文）。
- 日志先行：每次搜索先落 SearchRun 再执行，失败也留痕。
- Provider 基于 bing/360 页面抓取（今日实测 200），不引入收费 API。
