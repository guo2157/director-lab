# 06 连续性规则（Narrative Continuity）

> 事实来源：`backend/app/services/series/narrative_continuity_service.py`（268 行）、`backend/app/services/series/state_commit_service.py`（315 行）。

## 0. 概念澄清（必须先读）

系统内存在两个**完全独立**的「连续性」，不可混用：

| 概念 | 代码位置 | 含义 |
|------|----------|------|
| `EpisodeContinuity` | `app/services/continuity.py`、`episode_continuity` 表 | **视频首尾帧视觉连续性**，与剧情无关 |
| Narrative Continuity | `app/services/series/narrative_continuity_service.py` | **剧情连续性**，本节内容 |

本文件只讨论后者。系列模块**不读写** `episode_continuity`，不修改既有视频连续性逻辑。

## 1. 能力边界（代码注释原意）

服务只做**可确定性计算**的检查——所有结论都能由数据库中的结构化数据推导：

- 上一集是否已确认（草稿不能作为正式依据）
- 连续性快照是否存在
- 提案中的道具持有人是否与快照状态冲突
- 提案是否试图把已解决剧情线重新置为未解决
- 提案中的事件是否与既有确认事件重复
- 提案引用的资产是否属于该系列
- 新增资产名称是否与既有系列资产重名（只提示，不自动合并）

> **明确不做**：语义级剧情冲突（需要自然语言理解）不在本服务内伪造实现。上层 Agent 产出候选后，仍必须经过本服务的结构化校验与人工确认。

## 2. 严重级别

```python
SEVERITY_ERROR   = "error"    # 阻断：不处理不能确认
SEVERITY_WARNING = "warning"  # 警告：可显式忽略
SEVERITY_INFO    = "info"     # 提示：不影响确认
```

问题对象结构（`_issue` 生成）：

```json
{
  "severity": "error",
  "type": "prop_state_conflict",
  "asset_id": "ast_xxx",
  "message": "道具 ast_xxx 在最近快照中的持有人为 chr_a，本集提案变更为 chr_b",
  "source_event_ids": [],
  "suggestion": "补充道具转移动因与过程，或由用户确认历史状态是否需要修正"
}
```

## 3. 规则明细

### R1 上一集未确认（`previous_episode_not_confirmed`，warning）

判定：同系列中 `episode_number` 小于当前集的最大集号剧集存在，且其 `script_status != 'confirmed'`。

提示文案：`第 N 集剧本状态为 xxx，未确认草稿不能作为正式剧情依据`

建议：先确认上一集剧本，或在创作要求中明确本次仅做全新开场。

设计意图：**允许继续创作**（视频未完成不阻止下一集），但禁止把草稿当正式依据。

### R2 第一集（`first_episode`，info）

判定：不存在任何集号更小的剧集。

提示文案：`本集是本系列第一集，无历史剧情约束`

### R3 快照缺失（`continuity_snapshot_missing`，warning）

判定：`_latest_snapshot(series_id)` 为 None。

提示文案：`系列尚无正式连续性快照，本集将成为首个确认集`

### R4 无已确认事件（`no_confirmed_events`，info）

判定：该系列 `story_events` 中 `fact_status == 'confirmed'` 计数为 0。

提示文案：`系列当前没有任何已确认剧情事件`

### R5 道具持有人冲突（`prop_state_conflict`，error）

判定：提案 `props[]` 中某 `asset_id` 的 `holder_asset_id`，与最新快照 `state_json.props[asset_id].holder_asset_id` 同时存在且不相等。

提示文案：`道具 {asset_id} 在最近快照中的持有人为 {old}，本集提案变更为 {new}`

建议：补充道具转移动因与过程，或由用户确认历史状态是否需要修正。

> 剧情状态只存于 `continuity_snapshots.state_json`，**不写入 Asset 表**，避免资产属性被剧情污染。

### R6 已解决剧情线被重新打开（`plot_thread_reopen_conflict`，error）

判定：提案 `reopen_plot_thread_ids[]` 中存在状态为 `resolved` 或 `abandoned` 的剧情线。

提示文案：`剧情线「{title}」状态为 {status}，不能无理由重新变为未解决`

建议：如需重启该剧情线，请先由用户确认改为新的剧情线。

### R7 事件键缺失（`event_key_missing`，error）

判定：提案 `events[]` 中某条缺少 `event_key`。

提示文案：`事件候选缺少 event_key，无法保证幂等入账`

### R8 事件键重复（`event_key_duplicated`，error）

判定：同一提案内 `event_key` 重复出现。

提示文案：`事件键 {key} 在本次提案中重复出现`

### R9 资产未登记到本系列（`asset_not_in_series`，warning）

判定：事件 `involved_asset_ids[]` 中的资产既不在 `series_assets`，也不在 `episode_asset_refs`。

提示文案：`事件引用了未登记到本系列的资产 {asset_id}`

建议：先在共享资产中登记该资产，或确认是否为新建资产。

### R10 资产重名（`asset_name_duplicated`，warning）

判定：提案 `duplicate_asset_names[]` 中的名称与系列既有资产重名。

提示文案：`资产名称「{name}」与系列中已有资产重名，但名称相同不代表同一角色`

建议：请人工确认是否为同一资产，**禁止仅凭名称合并**。

> 这是硬约束：`assets/resolve` 只给候选提示，代码层面不做自动合并。

## 4. 状态汇总逻辑

```python
severities = {i["severity"] for i in issues}
if SEVERITY_ERROR in severities:
    status = "conflict"
elif SEVERITY_WARNING in severities:
    status = "warning"
else:
    status = "consistent"
```

| 结果 | 含义 | 能否确认 |
|------|------|----------|
| `consistent` | 无问题 | 可确认 |
| `warning` | 仅 warning/info | 需勾选 `ignore_warnings` 才能确认 |
| `conflict` | 存在 error | **不可确认**，必须修复 |

> 注意：代码实际判定为「存在 error 且未 `ignore_warnings` 则拒绝」。`ignore_warnings` 是让用户显式承认已知问题的开关，但不会让 error 静默通过——若确有 error，前端会提示先处理。

## 5. 预览与正式提交共用同一规则

`check_script_version(db, series, episode, proposal)` 同时被以下两处调用：

1. `GET /series/{id}/context-preview`、续写任务阶段三 → 只读预判
2. `POST /episodes/{id}/scripts/{script_id}/confirm` → 确认前最终校验

好处：预览看到的结果与确认时一致，避免「预览通过、确认失败」的落差（除状态版本变化外）。

## 6. 状态提交（State Commit）约束

`state_commit_service.confirm_script` 的执行顺序（**单事务**）：

1. `script_version.episode_id != episode.id` → `series_script_status_conflict`
2. 已 confirmed → **幂等重放**，返回 `idempotent_replay: true`，不重复入账
3. 状态不在 `draft` / `awaiting_review` / `analyzing` → 拒绝
4. `expected_state_version != series.state_version` → `series_state_version_conflict`（**乐观锁**）
5. `check_script_version(...)` → 存在 error 且未忽略 → 拒绝
6. 写入 `ContinuityChangeProposal`，`idempotency_key = confirm:{episode_id}:{script_version_id}`，唯一约束冲突 → `series_state_version_conflict`
7. `_apply_state_patch(base_state, proposal)` 生成新状态
8. `_persist_events(...)` 写入事件，返回 `(written, contradicted)`
9. 创建 `ContinuitySnapshot`（`version = previous + 1`，写 `state_hash`）
10. 剧本版本置 `confirmed`，同集其它 confirmed 版本置 `superseded`
11. 剧集 `script_status='confirmed'`、`confirmed_script_version_id`、`continuity_status=check['status']`
12. `series.latest_confirmed_episode_id` 更新，`series.state_version += 1`
13. 提案置 `committed`，`commit`

### 6.1 事件入账规则

| 输入 `fact_status` | 落库结果 |
|--------------------|----------|
| 未提供 | `confirmed` |
| `contradicted` | **不入账**，计入 `contradicted_event_keys` 由人工处理 |
| `confirmed` | `confirmed` |
| `derived` | `derived`（不算正式依据） |
| 其它 | `uncertain` |

`occurred_order` 从该系列现有最大值继续递增。同一 `(episode_id, script_version_id, event_key)` 已存在则跳过，保证幂等。

### 6.2 不变量

1. 同一时刻同一剧集只有 1 个 `confirmed` 剧本版本。
2. `series.state_version` 每次成功确认 +1，只增不减。
3. 快照 `version` 严格递增，不覆盖。
4. `idempotency_key` 全局唯一，重复提交不会产生第二次入账。
5. 视频生产完成（`mark_production_completed`）与剧本确认**完全解耦**，失败不影响剧本状态。

## 7. 历史剧本修改的影响分析（当前实现边界）

- 已确认剧本**不可原地修改**：需要修改必须新建草稿版本，再确认新版本；旧版本自动 `superseded`。
- 已入账的 `story_events` 不会因新版本确认而自动回滚或改写，历史事实保持可追溯。
- 因此「修改历史剧本导致后续剧集剧情失效」的情况，目前**不会自动分析**，只由连续性检查在下一次确认时以 warning 形式提示（如 R1、R9）。
- 该能力属于后续增强项，未在本期实现，文档不宣称已支持。

## 8. 规则与代码对照表

| 规则 | 类型常量 | 级别 | 代码位置 |
|------|----------|------|----------|
| R1 | `previous_episode_not_confirmed` | warning | `check_previous_episode` |
| R2 | `first_episode` | info | `check_previous_episode` |
| R3 | `continuity_snapshot_missing` | warning | `check_script_version` |
| R4 | `no_confirmed_events` | info | `check_script_version` |
| R5 | `prop_state_conflict` | error | `check_proposal` |
| R6 | `plot_thread_reopen_conflict` | error | `check_proposal` |
| R7 | `event_key_missing` | error | `check_proposal` |
| R8 | `event_key_duplicated` | error | `check_proposal` |
| R9 | `asset_not_in_series` | warning | `check_proposal` |
| R10 | `asset_name_duplicated` | warning | `check_proposal` |
