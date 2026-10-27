# Director-Lab Agent 实现独立审计报告

- 审计日期：2026-10-27 11:30–11:37 (UTC+8)
- 审计人：独立质量审计 Agent（与开发 Agent 非同一会话）
- 审计对象：`/home/ubuntu/director-lab`（main 分支，HEAD 0ede9d9，领先 origin/main 15 提交，含大量未提交改动）

## 1. 执行摘要

**整体结论：有条件通过。**

- 已实现并验证：导演台入口与三栏界面、会话/消息持久化、聊天接口（真实 LLM 调用）、模型选择器（真实生效）、受控只读工具注册表、项目上下文注入、分镜看板 episode 参数契约、资产提示词单一性模板、任务在途去重、合成/导出状态分离。
- 审计发现并修复 1 个 P1 缺陷（API-001 用户消息重复持久化），修复已验证。
- 环境受阻未验证：ComfyUI 实例不可达（HTTP 403，健康检查 degraded），视频生成/合成/导出真实链路无法端到端验证——属既有环境问题，非本次开发引入。
- 问题统计：P0=0，P1=1（已修复），P2=2，P3=1，Info=3。
- 可以进入受控使用（导演台对话/会话管理/项目状态查询）；视频生产链路需待 ComfyUI 实例恢复后回归。

**最主要三个风险：**
1. ComfyUI 实例不可达，视频链路全部未验证（环境类）。
2. 聊天接口为同步阻塞请求（LLM 超时 120s 内前端无反馈），长回复体验受限（P2）。
3. Agent 能力为首批范围（无附件、无审批事件、无意图路由、无记忆管理），与任务书完整愿景存在已知差距（Info，属分阶段计划内）。

## 2. 环境和证据

- 分支/提交：main @ 0ede9d9（工作区含未提交改动，审计全程未重置、未清理用户修改）
- 服务：uvicorn :8000（运行中）、worker scheduler（运行中）、vite dev :5174（5173 被同机 freellmapi 项目占用）
- 健康检查：`{"status":"degraded","database":"ok","comfyui":"unreachable","worker":"ok","disk":"ok"}`
- 数据库：SQLite(WAL)，Alembic head = b2c3d4e5f6a7（agent 表迁移已应用）
- LLM：OpenAI 兼容接口已配置（模型 deepseek-flash / deepseek-v4-pro 实测可用；密钥未泄露到任何审计输出）\n- ComfyUI：唯一启用实例 cloudstudio 地址不可达（403）——视频/TTS/合成链路验证受阻

**实际运行的命令与结果：**

| 命令 | 退出码 | 结果 |
| --- | --- | --- |
| `pytest tests/ -q`（后端全量） | 0 | 118 passed, 4 warnings, 22.10s |
| `npm run build`（vue-tsc + vite） | 0 | built in 6.37s（基线） |
| `npm run build`（API-001 修复后） | 0 | built in 6.47s |
| `curl /api/health` | 200 | degraded（comfyui unreachable） |
| `curl /api/projects/prj_fdc96b60281b/shots?episode=1` | 200 | 真实镜头数据 |
| `curl /api/projects/.../shots`（缺 episode） | 422 | 可诊断错误（query.episode Field required） |
| `alembic current` | 0 | b2c3d4e5f6a7 (head) |

## 3. 需求追踪矩阵（导演台相关）

| 需求 | 实现 | 测试方法 | 状态 |
| --- | --- | --- | --- |
| 首页「导演台」一级 Tab（品牌区与项目之间） | AppLayout topMenuOptions 首位 + /director 路由 | 代码核验 + vite 页面 200 | 已实现已验证 |
| 旧首页/路由无回归 | 全部旧路由保留 | 全量 pytest 118 passed + 构建 | 已验证 |
| 三栏布局（会话/对话/上下文） | DirectorConsoleView.vue | 代码核验 + API 实测 | 已实现已验证 |
| 会话持久化/刷新恢复 | agent_session 表 + API | API 往返测试（创建/列表/归档/恢复） | 已验证 |
| 消息持久化/seq 递增/增量加载 | agent_message 表 + (session_id,seq) 唯一索引 | pytest 8 用例 + API 实测 | 已验证 |
| 会话隔离（不串线） | 按 session_id 隔离 | 双会话并行发消息实测 | 已验证 |
| 归档软删除（消息保留） | status=archived | 归档后消息条数实测 | 已验证 |
| 聊天接口真实 LLM 调用 | LLMClient.chat_messages | 端到端实测（真实回复） | 已验证 |
| 模型选择真实生效 | 会话 model 字段覆盖 | 切换 deepseek-flash 后聊天 200 实测 | 已验证 |
| 模型选项来自服务端 | /settings/llm-models | 实测返回 2 个真实模型 | 已验证 |
| 受控工具注册表（只读） | agent_tools.py 3 工具 | pytest 7 用例 | 已验证 |
| 项目上下文注入 | 聊天时执行只读工具注入提示词 | 真实项目实测（clips 阶段 0/18 与库一致） | 已验证 |
| 附件上传与关联 | 未实现 | — | 未实现（阶段 D 剩余） |
| 意图路由/审批事件/记忆管理 | 未实现 | — | 未实现（阶段 E 剩余） |
| 流式输出 | 未实现（同步请求-响应） | — | 未实现（P2 记录） |
| 分镜看板 episode 契约 | 前端按集传参 | 正常 200 / 缺参 422 实测 | 已验证 |
| 资产提示词单一性 | 三类模板分立 | 模板代码核验（生成验证受阻） | 已实现，生成质量未验证（ComfyUI 不可达） |
| ComfyUI/H3 视频链路 | 既有实现保留 | 健康检查 unreachable | 环境受阻未验证 |
| 任务在途去重（幂等） | _check_inflight_duplicate | pytest（既有用例） | 已验证 |
| 合成/导出状态分离 | Composition/Export 独立模型 | pytest（既有用例） | 已验证 |

## 4. 缺陷清单

### API-001 [P1] [已修复] 用户消息重复持久化

- 位置：`frontend/src/views/DirectorConsoleView.vue` send()
- 复现：前端发送消息时先调 `POST /messages`（持久化用户消息），再调 `POST /chat`（后端再次持久化同一消息）
- 实测证据：复现会话中 seq 1、2 均为同一用户消息（『测试消息』出现两次）
- 影响：所有前端发送的消息在数据库中存储两份；LLM 历史窗口中出现重复消息，污染上下文
- 修复：send() 移除冗余 appendMessage 调用，直接使用 /chat 返回的 user_message；移除未使用导入（TS6133）
- 验证：修复后新会话仅 1 user + 1 assistant；前端构建通过（6.47s，退出码 0）

### FLOW-001 [P2] 聊天接口同步阻塞，无流式输出

- 位置：`backend/app/api/v1/agent.py` chat()
- 证据：接口同步等待 LLM 完整响应（llm_timeout_seconds=120），期间前端无中间反馈
- 影响：长回复时用户等待体验差；超时上限内连接占用
- 建议：接入既有 chat_stream + SSE（events 机制已存在），列为阶段 E 后续

### SEC-001 [P2] Agent 接口无身份认证（与全站一致）

- 证据：client.ts 注释『本期内网不鉴权』；agent 接口同样无鉴权
- 影响：内网部署下可接受；公网暴露时任何人可读写会话
- 判断：与项目既有安全基线一致（AGENTS.md P0-8 要求对外暴露必须先加鉴权），非本次开发引入的回归；公网部署前必须补鉴权

### UI-001 [P3] 右栏项目上下文在 ≤1024px 屏幕隐藏

- 位置：DirectorConsoleView.vue @media (max-width: 1024px) .context-panel { display: none }
- 影响：窄屏用户无法关联项目（功能降级为纯对话）
- 建议：改为可折叠抽屉而非直接隐藏

### Info 项（非缺陷）

- INFO-001：数据库中存在 7 个已归档的开发/审计测试会话（标题含『审计-』『验证』『冒烟』），无用户数据，保留不影响功能，可择期物理清理
- INFO-002：聊天历史窗口固定 40 条，无智能压缩（长会话早期上下文丢失，属设计取舍）
- INFO-003：浏览器端视觉/交互验证未执行（无浏览器自动化工具），布局结论基于代码核验与构建，标注为未完成视觉验证

## 5. 专项审计结果

- **前端与交互**：路由/菜单/三栏布局代码核验通过；旧页面无回归；视觉验证受工具限制未执行（INFO-003）
- **会话/消息持久化**：双会话隔离、归档保留、seq 唯一性实测通过；API-001 重复存储已修复
- **模型调用**：选项来自服务端真实配置；切换真实生效（实测）；密钥未泄露（前端代码/接口响应/日志均无凭据）
- **Agent 编排与工具权限**：工具注册表纯只读（无 db.add/commit/delete）；聊天接口仅以服务端持有的 project_id 调用 2 个固定工具，用户消息无法注入工具调用；未知工具/参数错误分类返回（pytest 覆盖）
- **终端安全**：无真实 Shell 执行能力（终端式交互未实现，无注入面）
- **人工审批**：未实现（阶段 E 剩余，无虚假审批按钮）
- **记忆与上下文恢复**：会话历史持久化可恢复（实测）；长期记忆未实现（无越权写入面）
- **数据库与 API 契约**：迁移 head 正确、表结构与模型一致、downgrade 完整；422 错误可诊断（含字段定位）；分页上限 100/200 合理
- **资产单一性**：角色（不出现第二人物/不手持剧情道具）、场景（不出现任何人物动物）、道具（不出现多个重复物品）三类模板约束齐全且分立；生成质量验证受阻（ComfyUI 不可达）
- **分镜看板**：episode 契约前后端一致，正常 200/缺参 422 可诊断，已知问题未复现
- **ComfyUI/H3**：实例不可达（403），真实生成验证受阻；调用链静态结构未改动（executor/comfyui_pool 保留）
- **连续性/合成/导出**：既有实现（episode_continuity 表、Composition/Export 分离、在途去重）保留且有测试覆盖；真实链路验证受阻
- **文档一致性**：AGENT_AUDIT_REPORT.md / AGENT_STAGE_BC_REPORT.md 与实际代码一致（本次审计逐项核对）

## 6. 测试结果

| 测试 | 环境 | 结果 |
| --- | --- | --- |
| 后端全量 pytest（118 用例） | 本地 venv | 118 passed（含 agent 15 用例） |
| 前端构建 vue-tsc + vite | Node | 通过（基线 6.37s / 修复后 6.47s） |
| 会话隔离/归档/恢复 API 往返 | 运行服务 | 通过 |
| 聊天端到端（真实 LLM） | 运行服务 | 通过（真实回复） |
| 模型切换端到端 | 运行服务 | 通过 |
| 分镜接口正常/异常路径 | 运行服务 | 200 / 422 可诊断 |
| API-001 修复验证 | 运行服务 | 通过（无重复消息） |
| 视频生成/合成/导出真实链路 | — | 未执行（ComfyUI 不可达，环境受阻） |
| 浏览器视觉验证 | — | 未执行（无浏览器自动化工具） |

## 7. 修复记录

| 缺陷 | 文件 | 改动 | 测试 | 回滚 |
| --- | --- | --- | --- | --- |
| API-001 | frontend/src/views/DirectorConsoleView.vue | send() 移除冗余 appendMessage；移除未使用导入 | 构建通过 + API 实测无重复 | remote_edit 撤销记录可用（file-undo-8e3c79e7 / 87da5486） |

未实施修复（需方案评审）：FLOW-001（流式输出，涉及 SSE 接入）、SEC-001（鉴权，涉及全站安全模型）、UI-001（窄屏抽屉）。

## 8. 上线/使用建议

**可进入受控使用：** 导演台对话、会话管理、模型选择、项目状态查询（只读工具）。

**上线阻断项（外部）：** ComfyUI 实例恢复前，视频/TTS/合成/导出链路不可用（健康检查 degraded）。

**高优先级后续：** FLOW-001 流式输出；阶段 D 剩余（附件）；阶段 E 剩余（意图路由/审批/记忆）。

**可延后：** UI-001 窄屏抽屉、INFO-001 测试会话物理清理、INFO-002 历史窗口压缩策略。

**公网部署前置条件：** 全站鉴权（SEC-001，项目既有基线要求）。

---

## 9. 增量审计修订（第二轮独立审计，证据驱动）

本轮由独立审计会话执行，在不覆盖既有结论的前提下交叉核对并补充以下已证实发现。既有结论中经复核一致的部分不再重复。

### 9.1 新增/升级缺陷

#### SEC-002 [P0，升级] 无鉴权 API 经公开前端代理对公网可达
- 证据（无损负向测试）：
  - `curl http://212.64.16.68:5174/api/health` → HTTP 200（公开可达）
  - `curl -X DELETE http://212.64.16.68:5174/api/projects/prj_nonexistent_probe` → HTTP 404 project_not_found（而非 401/403，证明写方法无鉴权可达；使用不存在 ID，未破坏任何真实数据）
- 根因链：
  1. 后端监听 0.0.0.0:8000 且全站无鉴权（SEC-001 已述）；
  2. `frontend/vite.config.ts` 将 `/api` 代理至 `http://127.0.0.1:8000`；
  3. 防火墙放行 TCP 5174（0.0.0.0/0），而 8000 未直接放行——鉴权缺失的实际暴露面经由 5174 代理放大到公网。
- 影响：任何公网用户可调用全部后端接口（读/写/删除/触发任务），包括删除项目、触发付费生成、读取会话与设置（密钥已掩码但业务数据无防护）。
- 与 SEC-001 的关系：SEC-001 判定为内网可接受，本轮证据表明实际已对公网暴露，故定级由 P2 升级为 P0。
- 修复建议（按风险递增择一或组合）：
  a) 最小化：防火墙/安全组收回 5174 公网放行，或仅放行到受控 IP；
  b) 直接：后端绑定 127.0.0.1 而非 0.0.0.0，前端 dev server 不对外；
  c) 根本：为全站 API 增加鉴权（项目 AGENTS.md P0-8 既有要求），并为公开入口加访问控制。
- 验证方法：修复后从公网重放上述两条 curl，期望健康检查不可达或返回 401/403。

#### API-003 [P1] 资产上传校验不足
- 位置：`backend/app/api/v1/assets.py` upload_asset。
- 证据与问题：
  1. 仅按请求体大小限制（500MB），未校验文件类型/MIME/后缀白名单，任意类型（含可执行文件）均可上传；
  2. 存储文件名取自客户端 `file.filename` 的 suffix，未做路径规范化清洗；
  3. 未先校验 project_id 真实存在即写入 Asset，缺少防线（当前库实测 0 孤儿，属防线缺失而非已发生）。
- 影响：恶意/异常文件入库、潜在路径与存储滥用、可产生孤儿资产。
- 建议：增加后缀/MIME 白名单；对 filename 做规范化（去除路径分隔符）；强制校验 project 存在性。
- 验证：负向用例（.exe、带 ../ 的文件名、不存在的 project_id）应分别返回 400/404。

#### SEC-003 [P2] 文档与代码不一致：ComfyUI 客户端实际使用 trust_env=True
- 证据：`backend/app/integrations/llm.py` 注释声称 trust_env=False（同 comfyui 客户端），但 `backend/app/integrations/comfyui.py:35` 实际为 `httpx.Client(..., trust_env=True)`。
- 影响：ComfyUI 请求会遵循本机 HTTP(S)_PROXY 等环境变量，存在被本机代理劫持/改道的风险；注释误导后续维护者以为两客户端行为一致。
- 建议：将 comfyui.py 改为 trust_env=False 与 llm.py 对齐（或明确记录需代理的例外），并更新注释。
- 验证：改动后测试通过，且代理变量不再影响 ComfyUI 连接。

#### SEC-004 [P2] 后端以 root 身份运行且监听 0.0.0.0
- 证据：`ps aux` 显示 uvicorn 进程属主为 root（`sudo -n nohup .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000`）。
- 影响：一旦出现文件写/RCE 类漏洞，直接影响面为 root 权限；最小权限原则未落实。
- 建议：以非特权用户（如 ubuntu）运行，仅放行必要端口。

#### DATA-001 [P3] 仓库残留文件与未忽略的敏感备份
- 证据：
  - 根目录存在误操作产生的文件 `View: 防抖(500ms)、行级loading、删除NPopconfirm确认、CSS网格列优化`（内容疑似某次 diff 文本）；
  - `backend/app/worker/executor.py.bak_20261026` 残留；
  - `backend/.env.bak` 存在且 `git check-ignore` 返回未被忽略（若提交将泄露环境配置）。
- 建议：清理误产生文件与 .bak；将 `*.bak`、`.env.bak` 加入 .gitignore；提交前复核。

### 9.2 本轮复核一致的既有结论（摘要）
- 分镜看板 episode 契约一致：缺参 422、带参 200，前端按集传参，已知问题未复现。
- 后端 pytest 118 passed（退出码 0）；前端 vue-tsc + vite build 通过（退出码 0）。
- 会话/消息持久化、seq 递增、归档软删除、模型切换真实生效、受控只读工具注册表：复核一致。
- 资产纯净性提示词三类模板分立且含负面约束，相关测试 10 例。
- 数据一致性：agent_session/agent_message/shot/asset/job/scene/character 外键检查 0 孤儿。
- 密钥掩码正确（settings 返回 ***），日志中疑似密钥行数统计为 0。

### 9.3 环境受阻未验证（保持）
- ComfyUI 实例不可达（健康检查 degraded）：视频生成/TTS/合成/导出真实链路仍无法端到端验证。
- 无浏览器自动化工具：视觉/交互层结论基于代码核验与构建，未完成视觉验证。

### 9.4 优先处理建议（按风险）
1. SEC-002（P0）：立即收敛公网暴露（收回 5174 放行或补鉴权），修复后从公网重放验证。
2. API-003（P1）：补齐上传白名单与 project 存在性校验。
3. SEC-003 / SEC-004（P2）：对齐 trust_env、后端降权运行。
4. DATA-001（P3）：清理残留文件并完善 .gitignore。

### 9.5 本轮修订后的分级统计（累计）
- P0：1（SEC-002，本轮升级）
- P1：2（API-001 已修复；API-003 待修复）
- P2：4（FLOW-001、SEC-001、SEC-003、SEC-004）
- P3：2（UI-001、DATA-001）
- Info：3

**修订后整体结论（未含用户裁决时）：不通过（可修复）。** 业务功能与测试基线通过，但存在已证实的公网无鉴权暴露（P0），在收敛该暴露前不应继续对公网提供服务。

### 9.6 风险接受决定与结论修订（用户裁决，2026-10-27 12:20）

- **决定**：本轮暂不处理 SEC-002（网络层公网暴露）与 API-003（上传白名单）。
- **记录方式**：9.1 中的原始证据与定级完整保留，不删除、不弱化已证实事实；本节仅调整处理优先级与整体结论。
- **明确接受的残留风险**：
  1. 5174 仍对 0.0.0.0/0 放行且 `/api` 代理至 127.0.0.1:8000，公网访问者可读写全部后端接口（含删除、触发任务）。仅在「临时验证期/该端口实际不应长期对公网可达」前提下可接受，验证结束应即时收回放行。
  2. 上传接口仍无后缀/MIME 白名单、无 filename 规范化、无 project 存在性前置校验。仅在「上传来源可信且为内部使用」时可暂时容忍。
- **恢复处理的最短路径（供后续）**：收回 5174 公网放行，或将后端绑定 127.0.0.1 且前端 dev server 不对外；上传增加后缀/MIME 白名单与 project 校验，并补负向用例。
- **修订后结论：有条件通过（受控使用）。** 业务功能、接口契约、数据一致性与测试基线均已验证通过；在明确接受上述两项残留风险的前提下，可进入受控使用。
- **修订后待处理优先级**：
  1. SEC-004 后端降权运行（P2，最小权限）；
  2. SEC-003 `comfyui.py` 的 `trust_env` 与 `llm.py` 对齐（P2，代理劫持风险 + 注释误导）；
  3. DATA-001 清理残留文件并为 `*.bak`、`.env.bak` 补 .gitignore（P3，防配置泄露）；
  4. FLOW-001 聊天流式输出（P2，体验项，非阻断）。
- **修订后分级统计（含风险接受）**：P0=1（SEC-002，已接受不处理）、P1=2（API-001 已修复 / API-003 已接受不处理）、P2=4（FLOW-001、SEC-001、SEC-003、SEC-004）、P3=2（UI-001、DATA-001）、Info=3。
- **未验证项保持不变**：ComfyUI 不可达导致视频/TTS/合成/导出链路未端到端验证；浏览器视觉验证未执行。此两项仍为受控使用的前置观察点。

---

## 10. 修复记录（第三轮实施，2026-10-27 12:23 起）

按用户裁决，本轮处理剩余 3 项低/中风险缺陷；SEC-002 与 API-003 按 9.6 节维持已接受风险，状态不变。

### FIX-SEC-003 [P2 → 已修复] ComfyUI 客户端 trust_env 与文档对齐
- 改动文件：`backend/app/integrations/comfyui.py:35`
- 改动内容：`httpx.Client(timeout=..., trust_env=True)` → `trust_env=False`
- 改前前置判断（避免打断渲染链路）：
  - 本机存在 `http_proxy/https_proxy/all_proxy=http://127.0.0.1:7890`，且 `no_proxy` 不含 ComfyUI 外部域名；
  - 以短超时分别探测同一 ComfyUI 地址：**带代理 code=403 time=5.05s / 直连 code=403 time=5.06s**，两条链路结果与耗时一致，证明代理并非该链路依赖，改动不会造成出网中断。
- 连带效果：`llm.py` 首部注释『trust_env=False（同 comfyui 客户端）』由原先的误导性描述变为准确，未再改动该注释。
- 验证证据：
  - `grep trust_env` 显示 comfyui.py:35 与 llm.py:76/119 全部为 `False`；
  - 后端全量 `pytest tests/ -q` → **118 passed**，退出码 0。
- 风险与回滚：低风险，仅影响 httpx 是否读取代理环境变量；回滚方式为恢复该行（file-undo-d519492e）。

### FIX-DATA-001 [P3 → 已修复] 清理仓库残留并补齐忽略规则
- 改动 1（配置）：`.gitignore` 新增 4 行——`# 备份文件（防止环境配置/代码备份误提交）`、`*.bak`、`.env.bak`。
- 改动 2（文件整理）：将两项残留移出工作区至备份目录 `/tmp/dlab_audit_backup_20261027/`（移动而非删除，可随时回滚）：
  - 根目录误操作产生的文本文件 `View: 防抖(500ms)、行级loading、删除NPopconfirm确认、CSS网格列优化`；
  - `backend/app/worker/executor.py.bak_20261026`。
- 刻意保留：`backend/.env.bak` 文件本身保留在磁盘（未删除），仅通过忽略规则阻断误提交路径。
- 验证证据：
  - `git check-ignore -v backend/.env.bak` → `.gitignore:31:.env.bak  backend/.env.bak`（规则生效）；
  - `git status --porcelain` 未跟踪列表中已不再出现上述两项 `.bak` 与误生成文件。
- 回滚：`mv` 回原路径即可；`.gitignore` 可用 file-undo-c549ad23 撤销。

### FIX-SEC-004 [P2 → 已修复] 后端降权为 ubuntu 用户运行
- 改前现状（只读确认）：
  - `uvicorn` 进程链为 `ubuntu bash → sudo -n → root → root(2590401)`，属主 root；
  - 无 systemd/supervisor 托管单元（仅临时 nohup 拉起），`sudo -n` 可用；
  - 日志 `uvicorn.log` 与数据目录 `data/`、`data/media/` 属主均为 ubuntu。
- 改动内容：终止 root 的 uvicorn 进程（2590398/2590400/2590401），以 **ubuntu** 身份重新以 `nohup .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000` 启动。
- 实施中发现的连带副作用（重要运维教训）：
  - 降权后健康检查的 `comfyui_instances` 由 1 个实例变为**空数组**，`/api/settings` 的 LLM 配置同步回退为空；
  - 根因：运行时配置文件 `backend/data/runtime_settings.json` 属主为 `root:root` 且权限 `600`，ubuntu 用户**无法读取**，导致 `comfyui_instances_json`、`llm_*` 等持久化配置静默丢失（应用不报错，仅回退默认值）；
  - 该类问题在『以 root 跑过再降权』的场景中具有普遍性：**配置文件由谁创建，决定了降权后能否读到**。
- 副作用修复：`chown ubuntu:ubuntu` 该文件并保持 `600`；同时将 root 属主的 `data/scheduler.log`、`data/worker.log`、`data/uvicorn.log` 一并改为 ubuntu 所有；随后重启后端使配置重新加载。
- 验证证据（降权闭环）：
  - 进程属主：`ps -eo pid,user,cmd` → uvicorn 属主为 `ubuntu`，且 `NO_ROOT_UVICORN`（无 root 残留）；
  - 健康检查：`/api/health` → 200，`comfyui_instances` 恢复为 1 个实例（`...cloudstudio.club`, weight 1），database/worker/disk 均 ok；
  - 配置回读：`llm_api_key_set=true`、`llm_model=deepseek-flash`、`llm_base_url` 非空（密钥仍以 `***` 掩码，未泄露）；
  - 核心接口冒烟：`/api/health`、`/api/projects`、`/api/agent/sessions`、`/api/settings`、`/api/jobs` 全部 200；
  - 日志可写：`test -w uvicorn.log` → 可写；
  - 回归测试：后端全量 `pytest` → **118 passed**，退出码 0。
- 残留观察项（新增建议，非本次阻断）：建议将 `backend/data/` 整目录属主统一为运行用户，并在部署文档中固化『以非 root 首次启动』的约定，避免后续再出现同类静默配置丢失。
- 回滚：如需恢复 root 运行，`sudo nohup ... uvicorn` 重启即可（不建议）。

### FLOW-001 [P2 → 仅方案，未实施] 聊天流式输出
- 现状：`POST /api/agent/sessions/{id}/chat` 为同步请求-响应，等待 LLM 完整回复，`llm_timeout_seconds=300` 期间前端无中间反馈，长回复体验差且连接长时间占用。
- 可复用基础（已存在，无需新建）：`LLMClient.chat_stream` 已支持 `on_delta` 回调与空闲超时保护；`app/api/v1/events.py` 已有 SSE 通道与事件表。
- 建议方案（最小改动路径）：
  1. 后端新增 `POST /api/agent/sessions/{id}/chat/stream`，复用 `chat_stream` 逐段推送 `delta` 事件，结束推送 `done` 并落库助手消息；
  2. 前端 `send()` 改用 `fetch` + `ReadableStream` 增量渲染，保留非流式接口作为降级；
  3. 失败时保持『用户消息已持久化、助手消息不落库』的既有语义，前端展示可重试错误码。
- 影响面与风险：涉及后端聊天主流程与前端消息渲染两处改造，属大范围变更；需保证幂等（重试不重复落库）与断连恢复（刷新后可取最终消息）。本轮未实施，建议单独排期并配套回归用例。

### 10.1 修复后的分级统计
- P0：1（SEC-002，已接受不处理）
- P1：2（API-001 已修复；API-003 已接受不处理）
- P2：4 → **已修复 2**（SEC-003、SEC-004）、**待排期 1**（FLOW-001）、**已接受 1**（SEC-001）
- P3：2 → **已修复 1**（DATA-001）、**待优化 1**（UI-001 窄屏抽屉）
- Info：3（不变）

### 10.2 当前可用性结论
- 导演台对话、会话管理、模型选择、项目上下文查询：已验证可用（降权重启后再次冒烟通过）。
- 后端运行身份、代理隔离、仓库卫生：已收敛至安全基线。
- 视频/合成/导出链路：仍因 ComfyUI 实例不可达（外部 403）无法端到端验证，属环境受阻，非代码缺陷。

---

## 11. 模型选择展示改造（第四轮，2026-10-27 13:28 起）

### 11.1 问题描述
- 用户诉求：导演台应展示配置的模型，并且可以选择。
- 审计核验结果：
  - 后端 `POST /api/settings/llm-models` 真实返回 `["deepseek-flash","deepseek-v4-pro"]`（HTTP 200），来源为 DeepSeek 的 `/models` 接口，**非硬编码**；
  - 当前配置 `llm_provider=deepseek`、`llm_model=deepseek-flash`、`llm_base_url=https://api.deepseek.com/v1`；
  - 但前端下拉框绑定的是 `activeSession?.model ?? null`，**新建会话 model 为空时只显示占位符「默认模型」，看不到实际生效的 `deepseek-flash`**。
- 结论：可选能力已具备且真实生效，缺的是「配置默认模型的可见性」。

### 11.2 实施中发现的后端缺陷（连带修复）
- 位置：`backend/app/api/v1/agent.py` `update_session`。
- 缺陷：原实现为 `if payload.model is not None: s.model = payload.model`，导致前端清空模型选择（传 `null`）时**永远不会生效**，会话会一直停留在旧模型上，无法回落到配置默认模型。
- 修复：改为 `if "model" in payload.model_fields_set: s.model = payload.model`——显式传 `null` 表示清空回落，未携带该字段则不变更。
- 改动量：+2 / −1，回滚记录 `file-undo-9a3ea6a4`。

### 11.3 前端改造
- 改动文件：`frontend/src/views/DirectorConsoleView.vue`（+28 / −5，回滚记录 `file-undo-6b48800b`）。
- 具体改动：
  1. 新增 `configModel` 状态，`loadModels()` 改为 `Promise.all` 并行请求「可用模型列表 + `/api/settings` 的 `llm_model`」，后者失败不阻断对话（`.catch(() => null)`）；
  2. 模型选项构建时，**配置默认模型置顶并标注「（默认）」**，其余可用模型去重后依次列出；
  3. 新增计算属性 `modelSelectValue = activeSession?.model ?? configModel ?? null`——会话级指定优先，否则展示配置默认值；
  4. 模板 `NSelect` 由 `activeSession?.model ?? null` 改为绑定 `modelSelectValue`，占位符动态显示「默认：deepseek-flash」；
  5. 清空选择后的提示语改为「已恢复默认模型：deepseek-flash」，明确回落目标。
- 语义保持：选择模型仍写入会话级 `model` 字段，后端按会话覆盖默认模型，真实影响后续调用；未选时使用配置默认模型。

### 11.4 验证证据
- 前端构建：`npm run build`（vue-tsc -b && vite build）→ **✓ built，退出码 0**；
- 后端回归：`pytest tests/ -q` → **118 passed**，退出码 0；
- 端到端 API 流程（重启后端后真实请求，会话 `ags_149d2c96990a`，验证完毕已归档）：
  | 步骤 | 请求 | 实际结果 | 判定 |
  | --- | --- | --- | --- |
  | 1 | 新建会话（不传 model） | `model = None` | 符合预期（回落默认） |
  | 2 | `PATCH {model:"deepseek-v4-pro"}` | `'deepseek-v4-pro'` | 会话级覆盖生效 |
  | 3 | `PATCH {title:"改名测试"}`（不带 model） | `model='deepseek-v4-pro'`，标题已改 | 未携带字段不变更，符合预期 |
  | 4 | `PATCH {model:null}`（显式清空） | `model = None` | 回落默认生效（修复前此步无效） |
  | 5 | `DELETE` 归档测试会话 | HTTP 200 | 清理完成 |
- 健康检查：重启后 `/api/health` → 200。

### 11.5 遗留说明
- 本轮修改后端 `agent.py` 后已重启 uvicorn（ubuntu 身份运行）使其生效，进程属主与配置加载均复核正常。
- 未执行浏览器视觉验证（无浏览器自动化工具），下拉框置顶标注与占位符文案的实际渲染效果未经视觉确认，结论基于代码核验与构建通过。
- 回滚方式：后端 `file-undo-9a3ea6a4`、前端 `file-undo-6b48800b`，两处需同时回滚才能恢复原行为。

---

## 12. 会话选中与项目关联缺陷修复（第五轮，2026-10-27 13:39 起）

### 12.1 用户报告的现象
- 当前会话为普通对话、未关联项目时，选择项目点击确认报错或无反应。
- 聊天输入框无法输入任何内容。

### 12.2 根因定位（两个症状同一处缺陷）
| 编号 | 等级 | 位置 | 根因 |
| --- | --- | --- | --- |
| UI-002 | P1 | `DirectorConsoleView.vue` `loadSessions()` | 判断写为『已有选中会话 **且** 该会话不在列表中』才回退选中首个；页面初次加载时 `activeSessionId` 为空，条件恒不成立 → **永不自动选中会话** |
| UI-003 | P1 | 同文件 `linkProject()` | 首行 `if (!activeSession.value) return` 静默返回，无任何提示；叠加 UI-002 后必然命中，用户点击「确认」无反馈，感知为报错 |
| API-004 | P1 | `backend/app/api/v1/agent.py` `update_session` | `if payload.project_id is not None` 导致显式传 `null`（取消关联）永远不生效 |

- 症状串联：无选中会话 → 输入框 `:disabled="!activeSessionId \|\| sending"` 恒为 true（输入框永久禁用）；右栏落入空分支并显示『当前会话为普通对话，未关联项目』（误导，实为一个会话都没选中）；点确认触发 `linkProject` 静默 return（无反馈）。
- 已排除的假设（均有证据）：后端接口不通（PATCH 实测 200）、字段缺失（`/api/projects` 含 `current_stage/shot_done/shot_total`）、前端编译失败（dev server 下发 http=200）、请求层异常（`client.ts` 的 `patch/del` 实现正常）。

### 12.3 修复内容
**前端** `frontend/src/views/DirectorConsoleView.vue`（两次改动，合计 +25 / −4）
1. `loadSessions()` 逻辑改为：加载完成后若「未选中」或「选中会话已失效」，自动选中列表第一个（回滚记录 `file-undo-5a2d18a0`）。
2. `linkProject()` 无会话时不再静默返回，改为提示『请先选择或新建一个会话，再关联项目』并退出选择态（回滚记录 `file-undo-cfc9ad31`）。
3. 右栏空状态文案按状态区分：无会话 → 『请先选择或新建一个会话』；有会话未关联 → 『当前会话为普通对话，未关联项目』。
4. 「关联项目」按钮在无会话时禁用，文案改为『请先选择会话』。

**后端** `backend/app/api/v1/agent.py`（+2 / −1，回滚记录 `file-undo-5165a298`）
5. `update_session` 的 `project_id` 改为 `model_fields_set` 判断，与 `model` 字段统一规则：显式 `null` = 取消关联，未携带该字段 = 不变更。

### 12.4 验证证据
- 前端构建：`npm run build`（vue-tsc -b && vite build）→ **✓ built，退出码 0**。
- dev server 实际下发模块已包含新代码：`stillExists` 命中 2 次、『请先选择或新建一个会话』命中 2 次（脚本 + 模板各一处）。
- 请求层确认：`client.ts` 中 `patch` 调 `apiClient.patch`、`del` 调 `apiClient.delete`，实现正常。
- 后端端到端往返（重启后真实请求，会话 `ags_ba067a055a90`，原始状态最后一并还原）：
  | 步骤 | 操作 | 结果 | 判定 |
  | --- | --- | --- | --- |
  | 0 | 读取初始状态 | `project_id='prj_fdc96b60281b'`, `model='deepseek-flash'` | 基线 |
  | 1 | `PATCH {project_id:null}` 取消关联 | `project_id = None` | **修复生效（修复前此步无效）** |
  | 2 | `PATCH {project_id:'prj_...'}` 重新关联 | `project_id = 'prj_fdc96b60281b'` | 关联可用 |
  | 3 | `PATCH {title:...}` 仅改标题 | `project_id` 与 `model` 均保持不变 | 未携带字段不变更，符合预期 |
- 后端回归：`pytest tests/ -q` → **118 passed**，退出码 0。
- 健康检查：重启后 `/api/health` → 200。

### 12.5 遗留与提示
- 本轮为加载后端修复而重启了 uvicorn，**当前进程仍以 root 运行**（PID 2643209，`sudo -n setsid nohup` 拉起）。此前 SEC-004 完成的降权已被外部重启覆盖，本轮按用户未授权范围未再次降权；若需保持降权基线，需重新以 ubuntu 身份启动，并注意 `data/runtime_settings.json` 的属主可读性（见第 10 章副作用）。
- 未执行浏览器视觉验证（无浏览器自动化工具）；输入框可输入、按钮态与提示文案的实际渲染未经视觉确认，结论基于代码核验、构建通过与真实接口往返。
- 回滚：前端 `file-undo-5a2d18a0` + `file-undo-cfc9ad31`，后端 `file-undo-5165a298`。
