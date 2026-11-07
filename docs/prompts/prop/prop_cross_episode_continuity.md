# 跨集道具连续性模板（prop_cross_episode_continuity）

同一项目中的同一道具可以跨集使用。本模板定义多集故事中道具状态的衔接规范，供 Prop Lifecycle Engine 与道具分析 Agent 遵循。

## 集序确定

1. 以 episode + shot 内序（scene_episode.seq_in_episode + shot.seq）共同确定播放顺序
2. 每集状态推导完成后写集末快照（prop_episode_snapshot）：该集最后一个镜头的四维状态（存在/持有/位置/物理）

## 下一集推导规则

1. **初始状态来源**：读取上一集的 prop_episode_snapshot.final_state，不依赖上一集最后一张图片
2. **默认延续**：上一集末态为 active 且携带/持有的道具，下一集首镜默认延续，除非剧情明确说明发生了变化
3. **变化必须有剧情依据**：新集首镜的道具状态突变（消失/换持有者/物理状态变化）必须有对应事件（含剧本证据），否则标记 unknown 待人工确认
4. **时间跳跃**：新集开头明确的时间跳跃（如『三天后』）由剧本分析标注，道具状态按剧情重新推导，人工确认后生效

## 跨集校验项

- 上一集末态携带的道具在新集首镜突然消失（无剧情依据）→ FAIL
- 上一集已 destroyed/consumed 的道具在新集完好出现（无 recovered/更换剧情）→ FAIL
- 持有者跨集突变且两角色无同场互动 → WARNING
- 道具外观约束跨集不一致（appearance_attrs 变化）→ WARNING

## 人工确认场景

- 时间跳跃导致的道具状态重置
- 集间道具更换（外观变化，需重新生成定妆图并更新 anchor_asset_id）
- 剧情明确说明的遗失与找回

## 与 EpisodeContinuity 的关系

- EpisodeContinuity 管理画面级首末帧传递（视觉参考）
- prop_episode_snapshot 管理剧情级道具状态（语义依据）
- 两者互补：分镜图生成的道具约束以生命周期状态为准，画面衔接以末帧为参考
