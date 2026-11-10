"""系列化创作服务包。

边界声明：
- `app/services/continuity.py` 负责跨集视频首尾帧连续性，不属于本包。
- 本包负责叙事层：系列管理、剧集管理、系列设定、上下文构建、剧情事实与连续性状态。

分层：
- series_service / episode_service / bible_service：基础 CRUD
- context_builder_service：确定性上下文构建（唯一入口）
- narrative_continuity_service：剧情一致性检查
- state_commit_service：正式状态提交（唯一写入方）
- plot_thread_service / asset_registry_service：剧情线与共享资产
"""

__all__: list[str] = []
