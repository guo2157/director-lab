# 角色-道具绑定关系与组合参考图管理方案（PROP_BINDING_PLAN）

生成时间：2026-11-06 ｜ 衔接：PROP_LIFECYCLE_PLAN（已落地 P0）

## 一、现状审查结论

### 1.1 已有能力（直接复用）

| 模块 | 位置 | 复用方式 |
|------|------|----------|
| 道具生命周期状态 | `prop_lifecycle_event/prop_shot_state`（已上线） | Reference Resolver 的状态输入源：existence/interaction/holder/visibility 直接决定参考图选择 |
| 道具锚定图 | `Prop.anchor_asset_id` + Asset 版本链（validity） | 独立道具参考图来源；validity≠current 视为失效 |
| 角色锚定图 | `Character.portrait_asset_id` | 独立角色参考图来源 |
| 语义槽位机制 | `ref_inject.build_media_params`（ref_identity/ref_character_N/ref_scene/ref_prop） | Workflow Adapter 的第一层：语义槽位分配 |
| 物理槽位映射 | `workflow_inject`（images.image_N 压缩重排 + `<imageN>` 重写，2026-11-06 修复） | Workflow Adapter 的第二层：按 Workflow JSON 实际输入节点动态映射 |
| 引用清单追溯 | `job_input_ref` 表（已建未启用）+ `input_snapshot.reference_layout` | Reference Manifest 的持久化载体 |
| 生成快照 | `input_snapshot`（params/workflow_version/comfyui_instance） | 扩展 reference_manifest / lifecycle_snapshot_version |
| 资产审核闸口 | `assets_gate` + `project.assets_review_status` | 组合图审核复用同闸口机制 |
| 事件审核队列 | `prop_lifecycle_event.review_status`（pending/confirmed/rejected） | 绑定关系的有效期判定依据（valid_from/until 挂事件） |

### 1.2 缺失能力

1. **无绑定关系数据**：角色与道具的持有关系只存在于生命周期事件的 holder 字段（按镜头快照），无独立可查询的『某角色在某剧情区间持有某道具』关系记录。
2. **无组合参考图**：资产库只有 character/scene/prop 三类锚定图，模型从未见过『小马背着麦袋』的组合图，多参考图只能靠模型自行理解关系。
3. **无组合图编译路径**：`compile_asset_image` 白名单仅 character/scene/prop（`ASSET_TEMPLATE_BY_TYPE`）。
4. **无统一 Reference Resolver**：选择逻辑分散在 shot_refs（启发式降级）+ prop_shot_state（状态驱动），缺一层『语义需求 → 资产查询 → 回退决策 → Manifest 输出』的集中引擎。
5. **无生成前参考图预览**：API 不返回本次将使用的参考图清单。

### 1.3 设计约束（整改要求）

- **不新增第四种基础资产类型**：character/scene/prop 保持一级分类；组合图是『关系』的附属视觉参考，作为 Asset 记录挂接在绑定关系上，`asset_type` 使用新值 `character_prop_binding` 但**不进入**前端资产库一级分类与 ASSET_TYPE 枚举展示分组，归入关系视图。
- 关系随剧情变化：绑定关系以生命周期事件为有效期边界（valid_from_event / valid_until_event），不允许永久固定。
- 图片选择逻辑不写进 LLM Prompt：LLM 只产出结构化需求（分镜涉及的角色/道具/关系），查询与绑定由 Resolver 代码完成。
- 先复用现有代码：槽位分配、审核闸口、快照结构、事件审核队列均复用，不重复建设。

## 二、数据模型（新增 1 表 + 1 枚举扩展）

### 2.1 新表 `character_prop_binding`（关系数据，cpb 前缀）

