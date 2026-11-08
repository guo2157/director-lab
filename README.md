# director-lab

自托管漫剧生产工作台：FastAPI + SQLite + Vue 3 前端，以本地 ComfyUI 为渲染底座。

## 项目描述

基于 ComfyUI 的 AI 漫剧生产工作台

完全免费 · 本地化部署：所有算力来自自建 ComfyUI 实例，无需云端 API，零持续费用。支持多台闲置 GPU 机器组池，配合内网穿透即可扩展。

三层负载均衡：实例健康熔断（心跳探测 + 自动摘除恢复）→ 实时负载调度（least-connections 按队列深度派发）→ 任务级故障转移（提交失败自动换实例重试），三重保障高可用。

## 项目结构

- `backend/`：FastAPI 后端（SQLAlchemy 2.0 + Alembic + Pydantic v2）
- `frontend/`：Vue 3 + TypeScript + Vite + Naive UI + Pinia + TanStack Query
- `docs/IMPLEMENTATION_PLAN.md`：开发实施计划与进度跟踪

## 环境要求

Python ≥ 3.11，Node.js ≥ 20，ffmpeg ≥ 6.0。

## 启动方式

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

## 测试

```bash
cd backend && .venv/bin/python -m pytest tests/ -v
cd frontend && npm run build
```
