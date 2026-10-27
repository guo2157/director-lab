# Director-Lab 智能导演台 · 阶段 A 项目审计报告

- 审计日期：2026-10-27
- 审计范围：`/home/ubuntu/director-lab` 全量代码、运行环境、文档与测试基线

## 1. 技术栈与启动方式（实测确认）

| 层 | 实际技术栈 |
| --- | --- |
| 后端 | FastAPI + uvicorn 单进程 + SQLAlchemy 2.0 + SQLite(WAL) + Alembic + Pydantic v2 |
| 前端 | Vue 3 + TypeScript + Vite + Naive UI + Pinia + TanStack Query + vue-router |
| 渲染 | ComfyUI 多实例池（comfyui_pool，加权轮询，单实例串行） |
| LLM | OpenAI 兼容接口（integrations/llm.py，凭据仅 DLAB_LLM_API_KEY 环境变量） |
| 任务 | 自研 worker scheduler（串行调度）+ SSE 事件推送（outbox + event_seq） |

运行中进程：uvicorn :8000（root）、app.worker.scheduler（root）、vite dev（5173）。健康检查 200。

启动方式（README）：后端 `uvicorn app.main:app --port 8000`；前端 `npm run dev`；测试 `pytest tests/`。

## 2. 现有模块与调用链

- API 层（app/api/v1/）：projects、script、characters、scenes、props、shots、assets、generate、jobs、compose、events(SSE)、settings、workflows 共 13 个路由域。
- 数据模型（18 表）：叙事 8（project/script_version/script_estimate/character/scene/scene_episode/prop/shot/episode_parse_record）+ 生产 10（asset/asset_ref/workflow/job/parse_version/dependency_ref/job_event/upload_record/composition/export）。
- 服务层：script_agent（评估+解析一体化）、script_parse、asset_prompts、ref_inject、workflow_inject、continuity、compose、image_quality、mock_render 等。
- worker：executor（图像/视频/TTS）、parse_executor、compose_executor、export_executor、scheduler。
- 短剧生产链路（实测可用部分）：项目创建 → 剧本评估/解析（LLM）→ 角色/场景/道具资产生成（ComfyUI）→ 分镜图（keyframe）→ 分镜视频（clip，ref2va/H3）→ TTS → 合成 → 导出。
- 前端页面：项目列表 / 剧本 / 解析 / 分镜看板 / 资产库 / 合成导出 / 任务中心 / 设置。导航为 AppLayout 顶部 n-menu（非项目页：项目/任务中心/设置；项目页：剧本/资产库/分镜看板/合成与导出）。

## 3. 已知缺陷核验

### 3.1 分镜看板『加载镜头失败：请求参数校验失败』

- 根因（已复现）：`GET /api/projects/{id}/shots` 与 `GET /api/scenes/{id}/shots` 的 `episode` 为必填 Query 参数（ge=1），缺失时 FastAPI 返回 422 `请求参数校验失败`。
- 当前工作区代码：StoryboardView 已按集加载（watch scenesQuery + activeEpisode，逐场景传 episode），实测 `GET /api/scenes/{id}/shots?episode=1` 返回 200。
- 结论：该缺陷在当前未提交代码中已修复；遗留风险是直接调用项目级 shots 接口未传 episode 的调用方。阶段 F 补回归测试。

### 3.2 资产主体污染（角色图混入多角色/场景混入人物等）

- 现状：asset_prompts.py 已有按资产类别的提示词模板（任务/参考图职责/必须保持/允许变化/禁止 结构），image_quality.py 有基础质量检查（空白图/过小图拒绝），tests/test_asset_purity.py 存在。
- 阶段 F 需逐模板核对单一性约束并补强。

### 3.3 外部依赖阻塞

- ComfyUI 实例失效（HTTP 403）：8 个 clip 任务、TTS、合成任务失败（comfy_unreachable 等）。实例恢复后可通过 jobs retry 重跑，去重逻辑已保证幂等。
- H3 硬件基准未校准（A10 单镜 20-25 分钟）。

## 4. 测试基线

- 后端 pytest：**103 passed**（2026-10-27 实测，含资产纯度、clip 注入与去重、合成导出等）。
- 前端构建：`npm run build`（vue-tsc + vite）为验收命令。
- 回归纪律：不得删除旧测试；新增功能必须补测试。

## 5. Agent 能力缺口（需新建）

当前代码库不存在：会话/消息持久化、导演台 UI、模型选择器（后端已有 /settings/llm-models 可复用）、附件上传关联、Agent 编排器（意图识别/规划/工具注册/审批/记忆）。均需在既有架构上增量实现，遵守 AGENTS.md P0 硬约束（技术选型不可改、长耗时操作 202+job+SSE、失效传播只标记不触发、密钥不入日志）。

## 6. Git 状态与保护措施

- main 领先 origin/main 15 个提交，工作区有大量未提交修改（MM 状态文件 10+）。
- 保护措施：所有改动严格增量、不回退用户未提交修改；每阶段完成后再评估提交点；不执行任何破坏性 Git 操作。

## 7. 分阶段实施计划

- 阶段 A（已完成）：审计与基线。交付物：本报告。
- 阶段 B：首页新增「导演台」一级 Tab（AppLayout 顶部菜单，logo 与「项目」之间）+ 三栏布局基础界面（左：历史会话；中：对话；右：项目与任务上下文）。验证：旧页面无回归，前端构建通过。
- 阶段 C：会话与消息持久化（新增 agent_session/agent_message 表 + Alembic 迁移 + API），刷新恢复、多会话隔离。
- 阶段 D：模型选择（复用 /settings/llm-models）、聊天接口（复用 LLMClient）、附件上传、工具调用展示。
- 阶段 E：Agent 核心（意图路由、计划、工具注册表、审批、记忆、异常诊断），复用现有 job/SSE/事件机制。
- 阶段 F：短剧工作流集成修复（资产模板单一性核验、分镜接口回归测试、H3 链路验证——受 ComfyUI 实例阻塞时如实标注）。
- 阶段 G：端到端验证、回归测试、文档更新、最终报告。

## 8. 主要风险

1. ComfyUI 实例失效阻塞视频链路验证（环境类，不可控）。
2. 工作区大量未提交修改，需严格增量开发避免冲突。
3. uvicorn/worker 以 root 运行，文件属主混合（root/ubuntu），新增文件需注意属主。
