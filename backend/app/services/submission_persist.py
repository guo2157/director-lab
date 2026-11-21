"""提交生成前把「本次实际下发的提示词 + 参考图槽位」回写为详情页数据源。

## 问题（2026-11-21，用户报障）

> 从第二镜起待生成的视频状态如果从待生成转化为视频生成中，这视频生成中之前有
> 调整提示词和参考图槽位的操作，然后提交生成视频，这生成视频前要先保存修改后的
> 提示词和参考图，这样用户查看分镜视频详情的时候就是看到对应生成的提示词和参考图，
> 槽位也能对应上。

实测确认这是**普遍存在的**缺陷，不是个别镜头：

| 入口 | 回写 shot.video_prompt | 保存 StoryboardRegenConfig |
|---|---|---|
| `POST /shots/{id}/regenerate/workspace/regenerate`（重新生成）| ✅ | ✅ |
| `POST /shots/{id}/generate`（**首次生成**，待生成→生成中）| ❌ | ❌ |

后果：详情页 `useShotDetail.open()` 读的是 `shot.video_prompt`，
`loadRefBindings()` 读的是 `StoryboardRegenConfig`。首次生成这两个都没写，
页面只能拿**解析阶段的原始文本**（Picture 标签数往往与真实槽位不符）和
**实时推导的槽位**（可能与本次提交时用户手改的不一致）来显示。

真实数据证据（修复前）：

```
job_05bd73d83fe0  sht_455fd0a48eec  status=running  saved_cfg=0
  job.prompt        tags=['1','2','3']  len=1224   ← 实际下发的
  shot.video_prompt tags=['1']          len=887    ← 详情页显示的
  两者相同: False
```

## 修法

**唯一事实来源 = 本次任务冻结的快照。** 提交时（`db.commit()` 之前）调用
`persist_submission()`，把同一份数据写进两个地方：

1. `shot.video_prompt` ← 最终下发的提示词（含 H3 三段式、含重建后的 `<Picture N>`）
2. `StoryboardRegenConfig` ← 与快照一致的 references（含压缩后的 `image_no`）

这样「用户看到的」与「实际生成的」在字节层面同源，不可能漂移。

### 为什么编号要一起回写

详情页 `image_no` 若来自工作流静态声明（1..9），而本次实际只注入了 3 张，
页面就会显示 Picture 2/3/4（有断档），与提示词里的 1/2/3 对不上。
回写时用 `reference_layout.compressed_numbers()` 按**实际注入槽位**算出的
连续编号，与执行器 `compact_reference_slots()` 完全同口径。
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models import Asset, Shot
from app.services import reference_layout as rl

logger = logging.getLogger(__name__)


def _slot_names(db: Session, project_id: str | None, media: dict[str, str]) -> dict[str, str]:
    """槽位 → 资产名（供职责块与页面展示使用）。

    路径分隔符历史数据里 `/` 与 `\\` 混用，直接用 `file_path == path` 等值查询
    会匹配不到，职责块退化成「@角色」，模型拿不到角色名。这里按 basename 归一化比对。
    """
    import os

    want = {
        os.path.basename(str(p or "").replace("\\", "/")): slot
        for slot, p in media.items()
        if p
    }
    if not want:
        return {}
    names: dict[str, str] = {}

    def _collect(query) -> None:
        for asset in query:
            base = os.path.basename(str(asset.file_path or "").replace("\\", "/"))
            slot = want.get(base)
            if slot and asset.name and slot not in names:
                names[slot] = asset.name

    if project_id:
        _collect(db.query(Asset).filter(Asset.project_id == project_id))
    if len(names) < len(want):
        _collect(db.query(Asset))
    return names


def build_references(
    db: Session,
    *,
    project_id: str | None,
    media: dict[str, str],
    declared_slots: list | None,
    workflow_id: str | None = None,
    workflow_version: int | None = None,
) -> list[dict]:
    """把本次实际注入的 `__media__` 转成工作台格式的 references（含压缩后编号）。

    与 `regen_workspace.build_references` 的字段保持一致，前端可直接消费。
    """
    numbers = rl.compressed_numbers(list(media.keys()), declared_slots)
    names = _slot_names(db, project_id, media)
    static = rl.static_numbers(declared_slots)
    decl_by_param = {
        item.get("param"): item
        for item in (declared_slots or [])
        if isinstance(item, dict) and item.get("param")
    }
    out: list[dict] = []
    for slot, image_no in sorted(numbers.items(), key=lambda kv: kv[1]):
        path = media.get(slot)
        asset = None
        if path:
            import os

            base = os.path.basename(str(path).replace("\\", "/"))
            asset = next(
                (
                    a
                    for a in db.query(Asset).order_by(Asset.version.desc())
                    if os.path.basename(str(a.file_path or "").replace("\\", "/")) == base
                ),
                None,
            )
        kind, _label = rl.slot_kind(slot)
        decl = decl_by_param.get(slot) or {}
        out.append({
            "reference_id": f"ref_{slot}",
            "slot_id": slot,
            "node_id": None,
            "input_key": rl.final_input_key(decl.get("input_key"), image_no),
            "image_no": image_no,
            # 与工作流静态声明的差异要显式留痕：排查时一眼看出「压缩过」
            "static_image_no": static.get(slot),
            "reference_type": {
                "prev_frame": "last_frame",
                "keyframe": "composition",
                "character": "character",
                "prop": "prop",
            }.get(kind, "other"),
            "asset_type": asset.asset_type if asset is not None else None,
            "asset_id": asset.id if asset is not None else None,
            "asset_name": names.get(slot) or (asset.name if asset is not None else slot),
            "character_id": None,
            "image_url": path,
            "binding_description": f"{names.get(slot) or slot} 的参考图",
            "identity_constraints": (
                ["保持角色原有外观", "保持体型与颜色", "保持五官特征"]
                if kind == "character"
                else []
            ),
            "required": False,
            "enabled": True,
            "sort_order": image_no,
            "semantic": "frame_constraint" if kind == "prev_frame" else "reference",
            "status": "confirmed",
        })
    return out


def persist_submission(
    db: Session,
    *,
    shot: Shot,
    project_id: str | None,
    target: str,
    prompt: str,
    media: dict[str, str],
    declared_slots: list | None,
    workflow_id: str | None = None,
    workflow_version: int | None = None,
    negative_prompt: str | None = None,
    parameters: dict | None = None,
) -> dict:
    """把本次提交的提示词与参考图槽位回写为详情页数据源。

    必须在 `db.commit()` **之前**调用，与任务快照同一次事务落库——
    否则可能出现「任务已入队、详情页还是旧值」的中间态。

    返回回写摘要（供任务事件与排查使用）。任何异常都不阻断生成：
    这是展示层一致性问题，不该让一次能跑的生成失败。
    """
    from app.models.regen_config import StoryboardRegenConfig
    from app.models import ids

    summary: dict[str, Any] = {"prompt_saved": False, "references": 0, "slot_numbers": {}}
    try:
        numbers = rl.compressed_numbers(list(media.keys()), declared_slots)
        summary["slot_numbers"] = dict(numbers)

        # 1. 提示词回写镜头：详情页 open() 读的就是这个字段
        if target == "clip" and prompt:
            shot.video_prompt = prompt
            summary["prompt_saved"] = True

        # 2. 参考图槽位回写工作台配置：详情页 loadRefBindings() 读的是这张表
        refs = build_references(
            db,
            project_id=project_id,
            media=media,
            declared_slots=declared_slots,
        )
        summary["references"] = len(refs)
        row = (
            db.query(StoryboardRegenConfig)
            .filter(StoryboardRegenConfig.shot_id == shot.id)
            .filter(StoryboardRegenConfig.target == target)
            .first()
        )
        now = __import__("datetime").datetime.now()
        if row is None:
            row = StoryboardRegenConfig(
                id=ids.new_id("rgc"),
                project_id=project_id or "",
                shot_id=shot.id,
                target=target,
                version=1,
                created_at=now,
            )
            db.add(row)
        else:
            row.version = int(row.version or 1) + 1
        row.workflow_id = workflow_id
        row.workflow_version = workflow_version
        row.references = refs
        # 提示词存最终下发版本：与 job.params.prompt 字节一致
        row.final_prompt = prompt if target == "clip" else row.final_prompt
        if negative_prompt is not None:
            row.extra = {**(row.extra or {}), "negative_prompt": negative_prompt}
        if parameters:
            row.parameters = parameters
        row.updated_at = now
        db.flush()
        summary["config_id"] = row.id
        summary["config_version"] = row.version
    except Exception:  # noqa: BLE001 - 展示层回写失败不得阻断生成
        logger.exception("提交回写详情页数据源失败 shot=%s target=%s", shot.id, target)
        summary["error"] = "persist_failed"
    return summary
