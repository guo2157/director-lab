# DEVELOPMENT —— 环境搭建、启动、测试与部署

> 本文档是开发环境搭建与日常运维操作的唯一权威定义（P1）。技术选型与版本下限见 ARCHITECTURE.md 第 8 节，备份恢复策略见 ARCHITECTURE.md 第 9 节，验收自测见 ACCEPTANCE.md。

## 1. 环境要求

| 依赖 | 版本下限 | 说明 |
| --- | --- | --- |
| Python | 3.11 | 后端运行环境 |
| Node.js | 20 | 前端构建 |
| ffmpeg / ffprobe | 6.0 | 媒体合成与探测，须可执行于 PATH |
| ComfyUI | 较新版本 | 外部独立服务，不随本项目部署；使用 H3 Partner Node 时需登录 Comfy 账号 |
| SQLite | 3.35+ | WAL 模式，由应用初始化时配置 |

## 2. 项目结构约定

待开发，推荐结构（实现时以本约定建仓）：

```
app/            # FastAPI 应用（HTTP 接口、SSE、静态文件托管）
worker/         # 单实例任务 worker（轮询执行、ComfyUI 提交、ffmpeg 合成）
alembic/        # 数据库迁移脚本
frontend/       # Vue 3 + Vite + TypeScript 前端
data/           # SQLite 数据库与媒体文件（按项目与类型分桶）
tests/          # 单元测试与集成测试
```

## 3. 启动与停止

启动顺序（P0，必须遵守）：

1. 确认 ComfyUI 服务可达（设置页测试或 `POST /api/settings/test-comfyui`）
2. **先启动 worker**，观察日志无异常（重点看首轮恢复扫描结果）
3. 再启动 app

停止顺序：先停 app（停止接新请求），再停 worker（等待 running 任务到终态或记录断点）。

进程管理：裸机部署下 app 与 worker 均由 systemd 管理，`Restart=always`、`RestartSec=5`；迁移 Docker Compose 后由容器 restart policy 接管。

启动自检：app 启动时比对 `alembic_version` 与代码期望版本，不一致拒绝启动并明确提示当前与期望版本（ARCHITECTURE.md 9.5 节）。

## 4. 数据库操作纪律（P0）

- 修改表结构必须写 Alembic 迁移脚本，且每个脚本必须提供 `downgrade` 实现
- 执行升级前必须先完成一次完整备份
- 数据库开启 WAL 且 worker 持续写入，**禁止在服务运行时直接拷贝主库文件与 `-wal`、`-shm` 文件**；一致性快照只能使用：
  - `sqlite3 data/app.db ".backup /backup/app-{ts}.db"`
  - `VACUUM INTO '/backup/app-{ts}.db'`
- 写事务最多等待 5 秒（busy_timeout=5000），单次请求内重试不超过 3 次，仍失败返回 503 `database_busy`

## 5. 测试

| 层级 | 要求 |
| --- | --- |
| 单元测试 | 核心业务逻辑（状态机流转、失效传播、时长规划校验、枚举归一化）必须有覆盖 |
| 集成测试 | API 层至少覆盖 ACCEPTANCE.md 中 TC-01 至 TC-39 的可自动化部分 |
| 冒烟测试 | 交付前按 ACCEPTANCE.md 第 3 节 10 步清单顺序执行，全部通过方可交付 |
| 工作流校验 | 修改或新增工作流 JSON 后，运行 `backend/scripts/validate_workflows.py <files...>` 静态校验 |
| 数据库备份 | POST /api/settings/backup 手动触发，备份文件落 backend/data/backups/；WAL 模式下生成 .db + .db-wal + .db-shm 三个文件，恢复时须一并还原到 data/ 目录 |

测试纪律：测试必须验证真实业务结果（状态、产物、数据库记录），不允许只验证 HTTP 200。

## 6. 日常运维操作

| 操作 | 方式 |
| --- | --- |
| 手动备份 | `POST /api/settings/backup`，或按计划任务每日自动执行 |
| 备份校验 | 备份完成后执行 `PRAGMA integrity_check`，核对媒体文件清单与 sha256 校验和 |
| 日志定位 | 按任务 ID 与工作流版本检索；密钥与敏感信息不得出现在日志中 |
| 清理上传缓存 | `POST /api/settings/clear-upload-cache`，不影响已入库资产 |
| 服务健康 | `GET /api/health`（database / comfyui / worker / disk 四项） |

## 7. 恢复流程（摘要）

完整流程见 ARCHITECTURE.md 9.4 节。关键步骤：停止 app 与 worker → 备份当前残留数据 → 恢复数据库文件并删除残留 `-wal`/`-shm` → 恢复媒体目录 → integrity_check 与文件清单比对 → 先启 worker 观察日志 → 再启 app → 验证项目列表与产物播放。

## 8. 外部服务配置

| 服务 | 配置位置 | 说明 |
| --- | --- | --- |
| ComfyUI | 设置页 ComfyUI 区 | base_url、可选 API Key、视频轮询超时（最小 60 秒，默认 300 秒） |
| 语言模型 | 设置页语言模型区 | OpenAI 兼容接口（base_url / api_key / model），供应商预设含 DeepSeek / OpenAI / 通义 / Ollama |
| 三方通道 | 设置页三方通道区 | 仅当本地 ComfyUI 不可用时按启用顺序降级 |

H3 Partner Node 会将参考图与提示词经 ComfyUI 发送至 Comfy/MiniMax 云端，设置页必须明示并可停用对应工作流（PRD.md 第 3 节已确认决策）。