# Prop Lifecycle（道具生命周期）整改方案

生成时间：2026-11-06 ｜ 状态：方案定稿，待分阶段实施

## 一、现状审查结论

### 1.1 现有可复用能力

| 模块 | 位置 | 复用方式 |
|------|------|----------|
| 道具实体模型 | `app/models/narrative.py::Prop` | 已有 name/description/appearance/is_key_prop/anchor_asset_id/normalized_name（跨集近名去重），扩展字段而非新建重复系统 |
| 道具资产锚定 | `Prop.anchor_asset_id` + Asset 版本链 | 直接作为道具外观权威来源（参考图绑定的 asset_id） |
| 资产计划依赖槽位 | `AssetPlanDependency`（plan_id+slot 唯一） | 语义绑定结构的现成雏形，扩展至分镜任务提交链路 |
| 参考图槽位机制 | `app/services/ref_inject.py`（_CHARACTER/_SCENE/_PROP_SLOTS、build_media_params 按类型分组、槽位不足报 422） | 保留，作为『语义绑定→槽位分配』的第二阶段执行器 |
| @引用解析 | `resolve_refs`（@道具名 → anchor_asset_id → file_path，缺失报错不静默） | 保留，作为绑定解析基础 |
| 跨集连续性框架 | `app/models/continuity.py::EpisodeContinuity` + `app/services/continuity.py` | 已支持集序、末帧传递、版本递增；道具跨集快照挂接此框架 |
| 剧本解析 | `app/services/script_parse.py`（LLM 输出 shot.props 数组，解析提示词已要求 props） | 分析任务拆分在此基础上扩展 |
| 分镜提示词注入 | `app/services/shot_refs.py`（inject_asset_refs/build_shot_asset_manifest） | 当前为『正文提及』启发式（2026-11-06 临时修复），P0 升级为生命周期状态驱动 |
| Agent 工具网关 | `app/services/agent_tool_impls.py`（TOOL_REGISTRY/execute_tool） | 道具分析 Agent 与校验 Agent 以新工具注册接入 |
| 审核闸口 | assets_gate（D-23）、keyframe_confirmed（D-13）、资产版本 validity | 状态修改保护已审核分镜的机制复用 |

### 1.2 关键缺口（整改对象）

1. **shot.props 落库丢失**：剧本解析 LLM 已输出每个 shot 的 props 数组，但 `script_parse.py` 落库 fields 构造未包含 props，数据在落库时被丢弃；Shot 表无 props 字段。
2. **无生命周期事件记录**：道具只有静态定义，无 introduced/picked_up/placed/transferred/destroyed 等事件，无持有者/位置/物理状态记录。
3. **无分镜级道具状态**：无法回答『第 N 镜时麦袋在哪、谁拿着、是否可见』。
4. **参考图绑定靠文字启发式**：当前按提示词正文是否提及道具名决定注入参考图，无剧情依据校验，误提/漏提均无法防护。
5. **无生成后视觉校验**：出图后无人检查道具是否出现/错画。
6. **无跨集道具状态快照**：EpisodeContinuity 只管末帧图片，不管道具状态。
7. **模板外置不完整**：docs/prompts/ 有道具资产图模板（compile_prop_asset/negative_prop），无生命周期分析/事件提取/校验模板。

## 二、数据模型设计（新增 3 表 + 2 处扩展，全部兼容现有数据）

### 2.1 新表 `prop_lifecycle_event`（事件表，只追加不覆盖）

``