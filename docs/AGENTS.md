# AGENTS.md —— AI 编程 Agent 开发总规则

> 本文件是 AI Agent 进入本项目的唯一入口。开始任何开发任务前必须先读完本文件，再按『任务 → 文档』索引只阅读与当前任务相关的文档，禁止每次通读全部资料。

## 1. 项目一句话

自托管漫剧生产工作台：FastAPI + SQLite + Vue 3 前端，以本地 ComfyUI 为渲染底座、ComfyKit API 格式工作流为可插拔能力单元，把剧本逐步转化为角色资产、分镜、H3 VA 音视频片段与可导出成片。

## 2. 文档索引（按任务选读）

| 当前任务 | 必读文档 |
| --- | --- |
| 任意开发任务 | 本文件 + DECISIONS.md（已确认决策与 TBD） |
| 实现产品功能（M1–M8） | docs/PRD.md |
| 状态机、任务队列、部署、备份 | docs/ARCHITECTURE.md |
| 建表、迁移、数据字段 | docs/DATABASE.md |
| 实现/核对 HTTP 接口 | docs/API.md |
| 工作流上传、校验、注入、H3 生成 | docs/WORKFLOW.md |
| 剧本解析、分镜设计（LLM 层） | docs/PROMPTS.md |
| 一致性度量、失效传播 | docs/CONSISTENCY.md |
| 前端页面、样式、交互 | docs/FRONTEND.md |
| 环境搭建、启动、测试、部署 | docs/DEVELOPMENT.md |
| 验收、测试用例、冒烟 | docs/ACCEPTANCE.md |

原始完整 PRD（唯一历史存档，不做日常开发依据）：`/home/ubuntu/prd/漫剧生产系统Prd-V1.2.3.md`。标准工作流 JSON 位于 `backend/data/workflows/`，校验脚本位于 `backend/scripts/`（清单见 docs/WORKFLOW.md）。

## 3. 不可违反的硬约束（P0）

1. **权威定义唯一**：接口以 docs/API.md 为准；表结构以 docs/DATABASE.md 为准；状态机以 docs/ARCHITECTURE.md 为准；工作流契约以 docs/WORKFLOW.md 为准。发现文档间冲突时必须停下来在 DECISIONS.md 记录并上报，禁止自行二选一静默实现。
2. **不得修改已确认技术选型**：FastAPI + uvicorn 单进程、SQLite + WAL、SQLAlchemy 2.0 + Alembic、Vue 3 + TypeScript + Naive UI + Pinia + TanStack Query、ComfyKit 客户端。版本下限：Python 3.11、Node 20、ffmpeg 6.0。
3. **并发 = 可用 ComfyUI 实例数（D-18 修订 D-03）**：单实例串行（ComfyUI 串行队列，取消接口 `/interrupt` 是全局中断）；多实例由实例池加权轮询分发并行执行，单实例同一时刻至多执行一个任务；禁止绕过实例池自行并行提交。
4. **事务纪律**：所有网络、上传、生成、下载、ffprobe、FFmpeg 调用必须在短数据库事务之外执行，禁止持事务等待远端。
5. **事件纪律**：任务状态与事件同事务写入 outbox，提交前禁止发布事件；事件必须带全局递增 `event_seq`，重复事件幂等。
6. **三套枚举禁止混用**：`asset_type` / `workflow_type` / `job_type` 语义独立，同名取值（如 clip）不得交叉赋值。
7. **失效传播只标记不触发**：上游变更将下游产物标 `stale`、镜头标 `redo`，禁止自动触发生成。
8. **密钥与素材安全**：密钥不明文入日志/错误详情；素材仅监听内网；对外暴露必须先加鉴权。
9. **TBD 不得当需求实现**：DECISIONS.md 中标记 TBD 的事项（如 T2V/FLF2V 动态输入键、H3 实际 FPS）禁止写成既定代码，实现时须留配置/回填入口。
10. **不得虚构**：接口、字段、文件路径、第三方能力均以文档与工作流 JSON 实际内容为准；无法验证的部分在代码注释与提交说明中标注『未验证』。

## 4. 开发通用规则

- ID 前缀：项目 `prj_`、场景 `scn_`、镜头 `sht_`、角色 `chr_`、道具 `prp_`、资产 `ast_`、任务 `job_`、工作流 `wf_`。
- 长耗时操作一律返回 202 + job 对象，经 SSE 推送结果；禁止同步阻塞等待生成完成。
- 排序参数仅接受各接口白名单字段，实现层用映射表转列名，禁止拼接请求值。
- 修改配置文件前先备份；修改表结构必须写 Alembic 迁移且提供 downgrade。
- 完成功能后必须对照 docs/ACCEPTANCE.md 中对应的 TC 用例自测。

## 5. 当前阶段

项目处于开发中期（2026-10-24）：阶段 1–3 已验证，阶段 4（任务编排与 ComfyUI 集成）代码已提交、待真实链路验证，阶段 5（前端联调）开发中；阶段 6–7 待开始。进度权威记录见 docs/IMPLEMENTATION_PLAN.md 第 2 节，决策与实证记录见 docs/DECISIONS.md。MVP 验收范围：单项目、单集、3–5 个镜头主链路（见 docs/ACCEPTANCE.md MVP 节）。
