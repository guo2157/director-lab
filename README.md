# director-lab

自托管漫剧生产工作台：FastAPI + SQLite + Vue 3 前端，以本地 ComfyUI 为渲染底座。

## 项目描述

基于 ComfyUI 的 AI 漫剧生产工作台

完全免费 · 本地化部署：所有算力来自自建 ComfyUI 实例，无需云端 API，零持续费用。支持多台闲置 GPU 机器组池，配合内网穿透即可扩展。

三层负载均衡：实例健康熔断（心跳探测 + 自动摘除恢复）→ 实时负载调度（least-connections 按队列深度派发）→ 任务级故障转移（提交失败自动换实例重试），三重保障高可用。

## 核心功能

- **剧本创作与评估**：AI 编剧（script_writing / script_evaluation），剧本解析、场景切分、意图路由与风格配置
- **分镜与资产管理**：分镜看板、资产计划（asset_plan）、角色/道具绑定与生命周期校验（prop_lifecycle / prop_validator）、参考图注入（ref_inject / shot_refs）
- **视频串行链式生成**：按镜头序号串行执行（chained 模式），前镜尾帧自动提取并绑定为本镜首帧引用（shot_chain），镜间连续性保障；全链路进度事件可观测，尾帧绑定失败自动级联取消防止缺参数生成
- **音频管线**：语音库匹配（voice_matching）、音频计划与片段状态管理、compose 时长对齐合成
- **一致性控制**：跨集连续性记录（continuity）、首帧使用追踪、图像质量门控（image_quality）、验收检查（acceptance_check）
- **重新生成工作台**：按资产/镜头维度的重生成编排（regeneration / regen_tools），版本号递增不覆盖历史
- **Agent 治理**：内置 AI 助手（parent_agent / agent_tools），带审批门控（approvals）、会话记忆（session_memory）、上下文工具与治理约束（agent_governance）
- **任务调度**：worker 调度器（scheduler）统一编排生成/合成/导出/解析执行器（executor / compose_executor / export_executor / parse_executor），支持重试熔断与对账续跑

## 项目结构

- `backend/`：FastAPI 后端（SQLAlchemy 2.0 + Alembic + Pydantic v2）
  - `app/api/v1/`：REST 接口层（projects / script / shots / asset_plans / generate / audio / compose / voice_matching / agent 等）
  - `app/services/`：业务服务层（剧本、分镜链、资产计划、语音匹配、一致性、Agent 等 60+ 模块）
  - `app/worker/`：后台调度器与任务执行器
  - `app/integrations/`：ComfyUI 客户端与实例池（comfyui_pool，含负载均衡与健康熔断）
- `frontend/`：Vue 3 + TypeScript + Vite + Naive UI + Pinia + TanStack Query
- `docs/`：架构与规范文档（ARCHITECTURE / API / DATABASE / WORKFLOW / AGENTS / CONSISTENCY 等），`docs/IMPLEMENTATION_PLAN.md` 为开发实施计划与进度跟踪

## 环境要求

Python ≥ 3.11，Node.js ≥ 20，ffmpeg ≥ 6.0。

## 启动方式

后端：

