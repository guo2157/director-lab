# director-lab

自托管漫剧生产工作台：FastAPI + SQLite + Vue 3 前端，以本地 ComfyUI 为渲染底座。

## 项目描述

基于 ComfyUI 的 AI 漫剧生产工作台

完全免费 · 本地化部署：所有算力来自自建 ComfyUI 实例，无需云端 API，零持续费用。支持多台闲置 GPU 机器组池，配合内网穿透即可扩展。

三层负载均衡：实例健康熔断（心跳探测 + 自动摘除恢复）→ 实时负载调度（least-connections 按队列深度派发）→ 任务级故障转移（提交失败自动换实例重试），三重保障高可用。

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

## 生产部署

生产环境采用 systemd 双服务 + 前端静态构建的方式部署（以下为本项目实际在用的配置）。

### 1. 后端服务（uvicorn）

创建 `/etc/systemd/system/director-lab-backend.service`：

```ini
[Unit]
Description=Director Lab Backend (uvicorn)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/director-lab/backend
Environment="NO_PROXY=localhost,127.0.0.1,::1"
Environment="PYTHONUNBUFFERED=1"
ExecStart=/home/ubuntu/director-lab/backend/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
Restart=always
RestartSec=3
StandardOutput=append:/var/log/director-lab/backend.log
StandardError=append:/var/log/director-lab/backend.log

[Install]
WantedBy=multi-user.target
```

说明：`NO_PROXY` 避免本机请求走代理；如服务器配置了全局代理，建议在服务单元中显式将代理环境变量置空，保证 ComfyUI 实例访问路径干净。

### 2. Worker 调度服务

创建 `/etc/systemd/system/director-lab-worker.service`：

```ini
[Unit]
Description=Director Lab Worker (scheduler)
After=network-online.target director-lab-backend.service
Wants=network-online.target

[Service]
Type=simple
User=ubuntu
Group=ubuntu
WorkingDirectory=/home/ubuntu/director-lab/backend
Environment="http_proxy="
Environment="https_proxy="
Environment="all_proxy="
Environment="HTTP_PROXY="
Environment="HTTPS_PROXY="
Environment="ALL_PROXY="
Environment="NO_PROXY=localhost,127.0.0.1,::1"
Environment="PYTHONUNBUFFERED=1"
ExecStart=/home/ubuntu/director-lab/backend/.venv/bin/python -m app.worker.scheduler
Restart=always
RestartSec=5
StandardOutput=append:/var/log/director-lab/worker.log
StandardError=append:/var/log/director-lab/worker.log

[Install]
WantedBy=multi-user.target
```

说明：Worker 与 Backend 分进程部署，Worker 在 Backend 之后启动；`Restart=always` 保证进程异常退出后自动拉起。代码更新后需重启 worker 才能加载新代码。

### 3. 前端构建

```bash
cd frontend
npm install
npm run build
```

构建产物输出到 `frontend/dist/`（`index.html` + `assets/`），由任意静态服务器或 nginx 托管，`/api` 反向代理到 `127.0.0.1:8000`。

### 4. 启用与日常运维

```bash
# 首次启用
sudo systemctl daemon-reload
sudo systemctl enable --now director-lab-backend.service director-lab-worker.service

# 代码更新后重启（worker 必须重启才能加载新代码）
sudo systemctl restart director-lab-backend.service
sudo systemctl restart director-lab-worker.service

# 查看状态与日志
systemctl status director-lab-backend director-lab-worker
tail -f /var/log/director-lab/backend.log /var/log/director-lab/worker.log
```

### 5. ComfyUI 实例池

- 实例地址在应用内运行时配置（`backend/data/runtime_settings.json`），可动态增删实例、切换实例并自动清理连接缓存，无需重启服务。
- 多台闲置 GPU 机器组池：每台机器运行 ComfyUI 服务，通过内网穿透（如 CloudStudio 代理域名）暴露地址后加入实例池即可扩展算力。
- 调度策略：实例健康熔断 → least-connections 负载派发 → 任务级故障转移，单实例宕机不影响整体生成。

## 测试

```bash
cd backend && .venv/bin/python -m pytest tests/ -v
cd frontend && npm run build
```
