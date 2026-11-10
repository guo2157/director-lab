# 07 迁移指南（系列化能力上线）

> 事实来源：`backend/alembic/versions/a1f4c8e2d907_series_management_tables.py`、`alembic heads` 实测输出、`backend/app/models/series.py`、`series_asset.py`、`series_story.py`。

## 1. 迁移基线

| 项 | 值 |
|----|-----|
| 迁移文件 | `backend/alembic/versions/a1f4c8e2d907_series_management_tables.py` |
| revision | `a1f4c8e2d907` |
| down_revision | `62f96f83efa9`（add_task_stage_agent_name） |
| 当前 head 实测 | `a1f4c8e2d907 (head)`，共 36 个 revision |
| 迁移类型 | **纯新增**：只 create_table / create_index，不改既有表 |

## 2. 变更内容

### 2.1 新增 11 张物理表

1. `series`
2. `series_episodes`
3. `series_bible_versions`
4. `series_script_versions`
5. `series_assets`
6. `episode_asset_refs`
7. `story_events`
8. `continuity_snapshots`
9. `continuity_change_proposals`
10. `episode_context_manifests`
11. `plot_threads`

> 计划中的 10 张业务表 + `episode_asset_refs` 独立成表，共 11 张。

### 2.2 新增索引与唯一约束

- 唯一约束 9 个，例如 `uq_series_episode_number`、`uq_series_script_episode_version`、`uq_story_event_episode_version_key`、`uq_continuity_proposal_idempotency`、`uq_context_manifest_episode_version`
- 普通索引 11 个，例如 `ix_series_status`、`ix_series_episodes_series`、`ix_series_episodes_status`、`ix_story_events_series_fact`、`ix_plot_threads_series_status`

详见 `02-database-design.md`。

### 2.3 明确不改动的内容

- 不新增、不修改、不删除任何既有表字段
- 不修改 `project`、`script_version`、`asset`、`storyboard`、`video` 等既有表
- 不迁移任何历史数据

## 3. 历史数据兼容策略

### 3.1 既有单集项目不迁移、不拆分

已在运行的单集项目**保持原样**：`project` 表不变，生产链路不变。系列侧通过 `series_episodes.project_id`（可空 FK）建立「剧集 → 生产项目」的引用关系，仅在需要时绑定。

### 3.2 历史多集项目

若某个既有 Project（或场景 `scene_episodes`）内包含多集内容，采用：

- **不拆分**历史数据结构
- 在 `series_episodes` 上用 `project_episode_no`（可空 Integer）记录「该剧集对应历史项目内的第几集」
- 不删除、不改写历史 Project 数据

### 3.3 命名冲突的处理决策

| 冲突点 | 决策 |
|--------|------|
| Episode 概念与既有 `scene_episodes` 冲突 | 新表命名为 `series_episodes` |
| 前端 `EpisodeOut` 与生产层 Episode 冲突 | 前端类型命名为 `SeriesEpisode` |
| 三种状态混用风险 | `script_status` / `production_status` / `Project.status` 三者彻底独立 |
| 视频首尾帧连续性冲突 | 新概念命名为「叙事连续性」，代码分属不同 service，互不调用 |
| 共享资产被复制 | 只登记关联（`series_assets`），禁止复制 `asset` 记录 |
| 前端路径冲突 | 新增 `/series` 前缀，不改既有项目路径 |

## 4. 升级步骤

### 4.1 前置检查

```bash
cd /home/ubuntu/director-lab/backend
.venv/bin/python -m alembic current
# 期望输出：62f96f83efa9

.venv/bin/python -m alembic heads
# 期望输出：a1f4c8e2d907 (head)
```

### 4.2 备份 SQLite 数据文件

```bash
cp backend/director_lab.db backend/director_lab.db.bak.$(date +%Y%m%d%H%M%S)
```

> SQLite 已开启 WAL，建议在服务静默期备份，或同时备份 `-wal` 与 `-shm` 文件。

### 4.3 执行升级

```bash
cd /home/ubuntu/director-lab/backend
.venv/bin/python -m alembic upgrade head
```

### 4.4 升级后验证

```bash
# 1. 版本确认
.venv/bin/python -m alembic current
# 期望：a1f4c8e2d907 (head)

# 2. 表存在性（SQLite）
.venv/bin/python - <<'PY'
import sqlite3
tables = ['series','series_episodes','series_bible_versions','series_script_versions',
          'series_assets','episode_asset_refs','story_events','continuity_snapshots',
          'continuity_change_proposals','episode_context_manifests','plot_threads']
con = sqlite3.connect('director_lab.db')
rows = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
for t in tables:
    print(('OK ' if t in rows else 'MISSING ') + t)
PY

# 3. 接口挂载确认
.venv/bin/python -c "
from app.main import app
p = app.openapi()['paths']
print('TOTAL', len(p))
print('SERIES', len([k for k in p if k.startswith('/api/series') or k.startswith('/api/episodes/') or k.startswith('/api/plot-threads')]))
"
# 实测：TOTAL 183

# 4. 系列链路测试
.venv/bin/python -m pytest tests/test_series_management.py -q
# 实测：22 passed

# 5. 全量回归
.venv/bin/python -m pytest -q
# 实测：25 failed / 592 passed（25 个为既有基线问题，已用 git worktree 基线复跑确认）
```

> 注意：heredoc 在本环境被禁用，实际执行时请使用 `-c "..."` 或临时脚本文件替代上述 `<<'PY'` 写法。

## 5. 回滚步骤

```bash
cd /home/ubuntu/director-lab/backend

# 1. 回滚一版（到 62f96f83efa9）
.venv/bin/python -m alembic downgrade 62f96f83efa9

# 2. 验证
.venv/bin/python -m alembic current
# 期望：62f96f83efa9
```

`downgrade()` 按逆序 drop_index + drop_table，**只删本迁移新增的对象**，不触碰既有表。

### 回滚影响

- 系列、剧集、Bible、剧本版本、事件、快照、提案、上下文清单、剧情线数据全部丢失
- 既有 Project / Script / Asset / Storyboard / Video 数据**不受影响**
- 单集生产链路可正常运行

### 回滚前必须确认

1. 已确认无正在运行的 `series_agent` Job（否则会写已删除的表）
2. 已备份需要保留的系列数据（如需）

## 6. 兼容性风险清单

| 风险 | 影响 | 缓解 |
|------|------|------|
| 既有测试基线 25 failed | 全量回归无法全绿 | 已用 git worktree 基线复跑确认为既有问题；本次新增测试 22 条全通过 |
| SQLite 并发写 | 高并发确认可能锁等待 | 单用户本地工具，风险可接受；确认流程为单事务 |
| `series_agent` Job 未完成即回滚 | 写表失败 | 回滚前检查无运行中 Job |
| 前端旧缓存 | 新路由 404 | 无需后端迁移，前端刷新即可 |

## 7. 上线顺序建议

1. 备份数据库
2. 停止后端服务（或确保无 `series_agent` Job 运行）
3. `alembic upgrade head`
4. 启动后端，确认 OpenAPI 183 条路径
5. 部署前端（新增 5 条路由）
6. 冒烟：创建系列 → 建剧集 → 上下文预览 → 续写 → 轮询进度 → 审核确认
7. 回归：既有项目页正常打开、剧本/分镜/资产/合成功能可用

## 8. 无破坏性验证记录

| 验证项 | 结果 |
|--------|------|
| 迁移 head | `a1f4c8e2d907 (head)` |
| 既有接口可用性 | OpenAPI 183 条，旧接口路径全部存在 |
| 系列专项测试 | 22 passed |
| 全量回归 | 25 failed / 592 passed（失败与改动前基线一致） |
| 迁移是否改动既有表 | 否，仅新增 |
