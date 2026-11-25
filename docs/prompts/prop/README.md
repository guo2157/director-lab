# docs/prompts/prop/ 目录状态说明

本目录下 7 个模板文件创建于 2026-11-07（提交 57284e9），是道具生命周期功能的
**设计阶段 LLM 提示词方案文档**，不是运行时模板。

## 定性依据（2026-11-25 审计确认）

- `backend/app/services/prop_lifecycle.py`、`prop_validator.py`、`api/v1/bindings.py`
  中无任何 LLM 调用：道具生命周期的事件提取、状态推导、校验全部以纯代码
  （正则匹配 + 状态机）实现。
- 全部 7 个文件在 backend/frontend 代码中零引用，未经 prompt_store 注册。

## 文件清单与对应代码实现

| 模板（设计文档） | 实际实现位置 |
|---|---|
| prop_extraction.md | script_parse.py 解析输出 props 字段 |
| prop_event_extraction.md | prop_lifecycle.py 规则事件推导 |
| prop_lifecycle_analysis.md | prop_lifecycle.py 状态机 |
| prop_continuity_prompt.md | prop_lifecycle.py + prompt_compiler 道具约束段 |
| prop_cross_episode_continuity.md | prop_lifecycle.py 跨集快照逻辑 |
| prop_state_validation.md | prop_validator.py 规则校验器 |
| prop_visual_validation.md | prompt_qc.py / keyframe_image_qc.py |

## 保留原因

作为功能设计的历史文档保留，供理解道具生命周期设计意图参考。
若未来计划将相关规则改回 LLM 方案，应先经 prompt_store 注册并建立一致性测试。
