# 道具状态校验规则模板（prop_state_validation）

用于 Prop Lifecycle Engine 连续性校验输出的问题清单解释与修复建议生成。

## 问题判定规则（通用）

| 问题码 | 判定条件 | 严重级别 | 修复建议方向 |
|--------|----------|----------|--------------|
| prop_seen_before_introduced | 镜头涉及道具但无 introduced 事件 | WARNING | 补充 introduced 事件（人工确认剧本原文），或确认该镜头不应涉及此道具 |
| prop_after_terminal | 道具处于 destroyed/consumed/lost 终态但镜头仍涉及 | FAIL | 确认是否有 recovered 事件或道具更换剧情；无则修改镜头道具标注 |
| held_without_holder | 道具被持有但无 holder 记录 | WARNING | 补充 picked_up/transferred 事件的 holder_after |
| prop_appear_without_evidence | 状态为 not_introduced 但生图提示词要求道具出现 | FAIL | 阻止生成，先补剧情依据 |
| holder_conflict | 事件持有者与镜头主说话角色冲突且无转交事件 | WARNING | 核对剧本原文，补充 transferred 事件 |
| cross_scene_carry_issue | carried 道具跨场景但持有者不在新场景 | WARNING | 检查剧情合理性，必要时置 placed + offscreen |
| physical_state_regression | 物理状态从终态回退（如 broken→intact）且无 repaired 事件 | FAIL | 补充 repaired/modified 事件或修正镜头描述 |
| cross_episode_discontinuity | 上一集末态与下一集首态突变且无剧情依据 | FAIL | 人工确认时间跳跃/道具更换，或补集间事件 |

## 输出要求

- 每条问题输出：problem_code / severity / shot_id / prop_id / message / suggestion
- message 使用中文，引用具体道具名与镜头号
- suggestion 必须给出可操作的修复动作（补充事件 / 修改镜头 / 人工确认）
- 不直接修改任何数据；修改由人工确认后经 Prop Lifecycle Engine 执行

## 画外与遮挡保护（禁止误判）

- 镜头未拍到道具 ≠ 道具消失：禁止输出 lost/destroyed 建议
- 道具被遮挡 ≠ 道具损毁：遮挡场景仅记录 visibility=occluded
- 时间跳转（time_jump=true）镜头状态突变：标记 unknown 待人工确认，不自动下结论
