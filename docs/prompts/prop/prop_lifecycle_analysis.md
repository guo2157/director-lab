# 道具生命周期分析模板（prop_lifecycle_analysis）

本模板定义道具生命周期分析的总体任务拆分与状态推导原则，供道具分析 Agent 与 Prop Lifecycle Engine 遵循。

## 任务拆分

### Task 1：道具提取

- 模板：prop_extraction.md
- 产出：道具实体属性档案（name/category/appearance_attrs/narrative_importance/initial_state）

### Task 2：道具使用事件提取

- 模板：prop_event_extraction.md
- 产出：prop_lifecycle_event 事件流（含剧本原文证据、持有者/位置/物理状态前后值）

### Task 3：道具生命周期推导

- 执行方：Prop Lifecycle Engine（derive_shot_states），非 LLM 直推
- 原则：
  - 状态只能从已确认事件推导，不能让生图模型自行决定
  - 无事件的镜头默认继承上一镜（continuity_from_shot 记录链路）
  - 但必须考虑镜头之间的时间变化（time_jump→unknown）、空间变化（场景切换时 placed 留原位、carried 跟随人）和剧情跳转
  - 不能机械地将上一镜头的状态复制到所有后续镜头
- 产出：prop_shot_state 分镜状态快照

### Task 4：连续性校验

- 模板：prop_state_validation.md
- 执行方：Prop Lifecycle Engine（validate_continuity）
- 产出：问题清单及修复建议（不直接改数据）

## 四维状态模型

1. **存在状态**：not_introduced / active / destroyed / consumed / lost / unknown
2. **持有关系**：unassigned / held / carried / placed / transferred（以 holder_id + position 记录，不靠文字描述）
3. **物理状态**：属性化 JSON（condition/content/...），允许扩展，不用固定枚举限制
4. **画面可见性**：visible / offscreen / occluded / unknown（与存在状态分离管理）

## 核心保护规则

- offscreen ≠ absent：当前画面没有出现道具，不代表道具已经不存在
- occluded ≠ destroyed：被遮挡不等于损毁
- 终态回退必须有 recovered 或人工确认事件
- 任何状态变化必须有剧情依据（事件证据）或人工确认
- LLM 推断与剧本原文冲突时，不修改原文，标记 pending 待人工确认

## 人工介入

- review_status=pending 的事件经人工确认（confirmed）后参与状态推导
- 人工修改事件使用 supersedes_id 指向被修正事件，历史保留
- 人工修改后必须重新推导受影响的后续镜头状态并重跑连续性校验
