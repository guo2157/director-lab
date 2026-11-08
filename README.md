# director-lab

自托管漫剧生产工作台：FastAPI + SQLite + Vue 3 前端，以本地 ComfyUI 为渲染底座。

## 项目描述

基于 ComfyUI 的 AI 漫剧生产工作台

完全免费 · 本地化部署：所有算力来自自建 ComfyUI 实例，无需云端 API，零持续费用。支持多台闲置 GPU 机器组池，配合内网穿透即可扩展。

三层负载均衡：实例健康熔断（心跳探测 + 自动摘除恢复）→ 实时负载调度（least-connections 按队列深度派发）→ 任务级故障转移（提交失败自动换实例重试），三重保障高可用。

## 项目页面
<img width="1898" height="822" alt="image" src="https://github.com/user-attachments/assets/5626cec2-887a-42cb-84ca-ca3adbedbf4a" />
<img width="1879" height="884" alt="image" src="https://github.com/user-attachments/assets/6303ada9-302f-4a2f-9257-1b389547d94b" />
<img width="1813" height="821" alt="image" src="https://github.com/user-attachments/assets/5075c938-da08-476c-bf6e-2af93699ed5f" />
<img width="1830" height="897" alt="image" src="https://github.com/user-attachments/assets/89681156-740a-4578-94f1-26f0c4fb5b6f" />
<img width="1832" height="824" alt="image" src="https://github.com/user-attachments/assets/15b6cfb5-a4c6-4a01-9ffd-67e5119f852a" />
<img width="1829" height="792" alt="image" src="https://github.com/user-attachments/assets/041dc867-d9cb-473b-97e5-b4aebfa465a5" />
<img width="1817" height="773" alt="image" src="https://github.com/user-attachments/assets/1cc63ef9-4224-416c-b6cd-d4d97e870abd" />
<img width="1835" height="837" alt="image" src="https://github.com/user-attachments/assets/1020c0d6-ab79-4a5c-9ccc-ca8905c28018" />
<img width="1805" height="765" alt="image" src="https://github.com/user-attachments/assets/60452a89-f8a7-4001-b4bf-0386c01a928f" />
<img width="1868" height="843" alt="image" src="https://github.com/user-attachments/assets/71edf673-6172-4086-89c5-b8852ebbf570" />
<img width="1866" height="782" alt="image" src="https://github.com/user-attachments/assets/b2ff3953-5aac-4d68-b74b-2f1f288d4f0e" />
<img width="1886" height="864" alt="image" src="https://github.com/user-attachments/assets/66178fc3-6226-462b-861a-03e2080e7a63" />
<img width="1872" height="780" alt="image" src="https://github.com/user-attachments/assets/1f306998-a78f-4bbd-94bd-a0b3715e28e9" />
<img width="1895" height="855" alt="image" src="https://github.com/user-attachments/assets/4e38134c-aba2-41d6-a895-766af52edc87" />


## 核心功能

- 剧本创作与评估：AI 编剧（script_writing / script_evaluation），剧本解析、场景切分、意图路由与风格配置
- 分镜与资产管理：分镜看板、资产计划（asset_plan）、角色/道具绑定与生命周期校验、参考图注入（ref_inject / shot_refs）
- 视频串行链式生成：按镜头序号串行执行（chained 模式），前镜尾帧自动提取并绑定为本镜首帧引用（shot_chain）；全链路进度事件可观测，尾帧绑定失败自动级联取消防止缺参数生成
- 音频管线：语音库匹配（voice_matching）、音频计划与片段状态管理、compose 时长对齐合成
- 一致性控制：跨集连续性记录、首帧使用追踪、图像质量门控（image_quality）、验收检查（acceptance_check）
- 重新生成工作台：按资产/镜头维度的重生成编排，版本号递增不覆盖历史
- Agent 治理：内置 AI 助手，带审批门控、会话记忆与治理约束
- 任务调度：worker 调度器统一编排生成/合成/导出/解析执行器，支持重试熔断与对账续跑

## 项目结构

- `backend/`：FastAPI 后端（SQLAlchemy 2.0 + Alembic + Pydantic v2）
  - `app/api/v1/`：REST 接口层（projects / script / shots / generate / audio / compose / voice_matching / agent 等）
  - `app/services/`：业务服务层（剧本、分镜链、资产计划、语音匹配、一致性、Agent 等 60+ 模块）
  - `app/worker/`：后台调度器与任务执行器
  - `app/integrations/`：ComfyUI 客户端与实例池（comfyui_pool，含负载均衡与健康熔断）
- `frontend/`：Vue 3 + TypeScript + Vite + Naive UI + Pinia + TanStack Query
- `docs/`：架构与规范文档（ARCHITECTURE / API / DATABASE / WORKFLOW 等），`docs/IMPLEMENTATION_PLAN.md` 为开发实施计划

## 环境要求

Python ≥ 3.11，Node.js ≥ 20，ffmpeg ≥ 6.0。

## 启动方式（开发）

后端：

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

前端：

```bash
cd frontend
npm install
npm run dev
```

开发服务器默认 5173 端口，/api 请求代理到 127.0.0.1:8000。

## 配置

所有可配置项集中在 `backend/app/core/config.py`，以 `DLAB_` 前缀环境变量覆盖（ComfyUI 地址、一致性双阈值、H3 参数、时长上限等）。

ComfyUI 实例池支持运行时配置（`backend/data/runtime_settings.json`），可动态增删实例地址、切换实例并自动清理连接缓存，无需重启服务。

###  ComfyUI 实例池

- 实例地址在应用内运行时配置（`backend/data/runtime_settings.json`），可动态增删实例、切换实例并自动清理连接缓存，无需重启服务。
- 多台闲置 GPU 机器组池：每台机器运行 ComfyUI 服务，通过内网穿透（如 CloudStudio 代理域名）暴露地址后加入实例池即可扩展算力。
- 调度策略：实例健康熔断 → least-connections 负载派发 → 任务级故障转移，单实例宕机不影响整体生成。

## 测试

```bash
cd backend && .venv/bin/python -m pytest tests/ -v
cd frontend && npm run build
```
