"""对话上下文用只读工具编排（自 agent_tools 拆分）。

职责：按用户消息挑选只读工具调用并格式化为提示词上下文；
依赖 agent_tools 的工具实现与结果类型，本模块不含任何写操作。
"""
from sqlalchemy.orm import Session

from app.services.agent_tools import (
    ToolError,
    ToolResult,
    tool_list_project_entities,
    tool_list_project_shots,
)


# --- 对话上下文用只读工具编排（Agent chat 注入上下文时调用） ---

# 进度类关键词：命中即注入三张进度表（生成进度是最高频问题）
_PROGRESS_HINTS = (
    "进度", "进展", "到哪", "哪一步", "还有多少", "还剩", "排队", "多久", "什么时候",
    "生成", "出图", "渲染", "完成", "状态",
)
# 实体/镜头定位关键词：命中即注入角色/场景/道具与镜头清单（把名称解析成 ID，避免反问）
# 只收领域名词+定位词，避免「哪些/哪个」这类泛化词把无关问题也拖进实体查询
_ENTITY_HINTS = (
    "角色", "场景", "道具", "人物", "镜头", "第几镜", "哪一镜", "第几集", "谁", "资产", "定妆",
    "基准图", "锚定",
)
# 疑问词 + 实体对象才算「要列实体」：『有哪些角色』命中，『导出记录有哪些』不命中
_ENTITY_LIST_PATTERN = __import__("re").compile(
    r"(有哪些|哪几个|列出|所有|全部|哪些)(角色|场景|道具|人物|镜头|资产)"
)
# 解析与成片关键词
_PARSE_HINTS = ("剧本", "解析", "分析", "场景", "镜头", "资产定义", "集")
_COMPOSE_HINTS = ("合成", "导出", "成片", "交付", "视频文件", "下载", "验收")
_JOB_HINTS = ("失败", "报错", "错误", "重试", "卡住", "超时", "失败原因")
# 镜头序号：只认『第 N 镜 / 第 N 个镜头』，避免『第一帧』『第三集』误命中
_SHOT_SEQ_PATTERN = __import__("re").compile(r"第\s*(\d+)\s*(?:个)?\s*镜头?")
# 集数：『第 2 集』
_EPISODE_PATTERN = __import__("re").compile(r"第\s*(\d+)\s*集")


def build_read_tool_calls(text: str, project_id: str) -> list[tuple[str, dict]]:
    """按用户消息挑选要执行的只读工具（返回 [(tool_name, args)]）。

    - 进度类问题：三张进度表全部注入（回答进度必须准确到 X/Y、正在生成哪几个、排队与预计耗时）
    - 实体/镜头类问题：注入项目实体与镜头清单，用于把名称/镜号解析成 chr_/scn_/prh_/sht_ ID
    - 解析/合成/导出/失败诊断：按关键词补充
    - 不做任何写操作：这里只返回只读工具
    """
    content = text or ""
    calls: list[tuple[str, dict]] = []

    if any(k in content for k in _PROGRESS_HINTS):
        for tool in (
            "get_asset_generation_progress",
            "get_keyframe_generation_progress",
            "get_clip_generation_progress",
        ):
            calls.append((tool, {"project_id": project_id, "failed_hours": 24}))

    # 『第 3 镜』『第 2 集』这类明确指代也算要定位镜头/实体（否则助手答得对但说不出是哪一镜）
    wanted_entities = (
        any(k in content for k in _ENTITY_HINTS)
        or bool(_ENTITY_LIST_PATTERN.search(content))
        or bool(_SHOT_SEQ_PATTERN.search(content))
        or bool(_EPISODE_PATTERN.search(content))
    )
    if wanted_entities:
        calls.append(("list_project_entities", {"project_id": project_id, "limit": 60}))
        shot_args: dict = {"project_id": project_id, "limit": 60}
        seq_m = _SHOT_SEQ_PATTERN.search(content)
        if seq_m:
            shot_args["shot_seq"] = int(seq_m.group(1))
        ep_m = _EPISODE_PATTERN.search(content)
        if ep_m:
            shot_args["episode"] = int(ep_m.group(1))
        calls.append(("list_project_shots", shot_args))

    if any(k in content for k in _PARSE_HINTS):
        calls.append(("get_parse_status", {"project_id": project_id}))

    if any(k in content for k in _COMPOSE_HINTS):
        calls.append(("list_compositions", {"project_id": project_id, "limit": 5}))
        calls.append(("list_exports", {"project_id": project_id}))

    if any(k in content for k in _JOB_HINTS):
        # 失败诊断：取最近失败任务再看详情，给出具体原因而不是「有失败」
        calls.append(("list_jobs", {"project_id": project_id, "status": "failed", "limit": 5}))
    return calls


def entity_shot_hint(db: Session, project_id: str, *, entity_limit: int = 12) -> str:
    """给澄清消息附上可选的实体与镜头清单，让用户（或下一轮识别）知道有哪些 ID 可选。

    生成目标定位不到时使用：既不猜测目标，也不把「自己去找 ID」的负担丢给用户。
    """
    try:
        entities = tool_list_project_entities(db, project_id, limit=entity_limit)
        shots = tool_list_project_shots(db, project_id, limit=entity_limit)
    except ToolError:
        return ""
    lines: list[str] = []
    if entities.get("items"):
        lines.append(
            "项目内可选实体："
            + "、".join(
                f"{it['name']}（{it['type']} {it['id']}）" for it in entities["items"][:entity_limit]
            )
        )
    if shots.get("items"):
        lines.append(
            "项目内可选镜头："
            + "、".join(
                f"第{it['episode']}集 {it['scene_title']} 镜{it['seq']}（{it['shot_id']}）"
                for it in shots["items"][:entity_limit]
            )
        )
    return "\n".join(lines)


def _short(value, limit: int = 120) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_tool_context(tool_name: str, result: ToolResult) -> str:
    """把只读工具结果压成一行提示词上下文（与页面展示口径一致，便于助手直接复述）。"""
    if not result.ok or not result.data:
        return ""
    d = result.data

    if tool_name == "get_asset_generation_progress":
        groups = d.get("groups") or {}
        group_text = "、".join(
            f"{g.get('label', k)} {g.get('done', 0)}/{g.get('total', 0)}"
            for k, g in groups.items()
            if g.get("total")
        )
        line = f"公共资产图进度：{d.get('done', 0)}/{d.get('total', 0)}（{d.get('percent', 0)}%）"
        if group_text:
            line += f"；分组：{group_text}"
        inflight = d.get("inflight") or []
        if inflight:
            line += "；生成中：" + "、".join(
                f"{j.get('kind_label') or j.get('kind')}·{j.get('entity_name')}"
                f"（{j.get('status')}{' ' + str(j.get('progress')) + '%' if j.get('progress') is not None else ''}）"
                for j in inflight[:6]
            )
        if d.get("failed_recent_count"):
            line += f"；最近失败 {d['failed_recent_count']} 个：" + "、".join(
                f"{f.get('entity_name')}（{_short(f.get('error_msg'), 60)}）"
                for f in (d.get("failed_recent") or [])[:3]
            )
        if d.get("all_done"):
            line += "；已全部完成"
        return line

    if tool_name == "get_keyframe_generation_progress":
        line = (
            f"分镜图进度：已出图 {d.get('with_keyframe', 0)}/{d.get('total', 0)}"
            f"（{d.get('percent', 0)}%），已人工确认 {d.get('confirmed', 0)}"
        )
        inflight = d.get("inflight") or []
        if inflight:
            line += "；生成中：" + "、".join(
                f"{j.get('scene_title') or '未知场景'} 镜 {j.get('shot_seq') or '—'}"
                f"（{j.get('status')}）"
                for j in inflight[:6]
            )
        if d.get("failed_recent_count"):
            line += f"；最近失败 {d['failed_recent_count']} 张：" + "、".join(
                f"{f.get('scene_title') or ''} 镜 {f.get('shot_seq') or '—'}（{_short(f.get('error_msg'), 60)}）"
                for f in (d.get("failed_recent") or [])[:3]
            )
        return line

    if tool_name == "get_clip_generation_progress":
        line = (
            f"分镜视频进度：已生成 {d.get('with_clip', 0)}/{d.get('total', 0)}"
            f"（{d.get('percent', 0)}%），渲染中 {d.get('inflight_running', 0)}"
            f"，排队/等待前序 {(d.get('inflight_queued') or 0) + (d.get('inflight_chained_wait') or 0)}"
        )
        running = next(
            (j for j in (d.get("inflight") or []) if j.get("status") == "running"), None
        )
        if running:
            line += (
                f"；当前渲染：{running.get('scene_title') or '未知场景'} 镜 {running.get('shot_seq') or '—'}"
                + (f" {running.get('progress')}%" if running.get("progress") is not None else "")
            )
        wait = d.get("estimated_wait_seconds")
        if isinstance(wait, int) and wait > 0:
            line += f"；预计剩余约 {max(1, round(wait / 60))} 分钟"
            if d.get("avg_seconds"):
                line += f"（近 7 日单镜平均 {d['avg_seconds']} 秒）"
        if d.get("failed_recent_count"):
            line += f"；最近失败 {d['failed_recent_count']} 个"
        if d.get("all_done"):
            line += "；已全部完成"
        return line

    if tool_name == "list_project_entities":
        items = d.get("items") or []
        if not items:
            return "项目暂无角色/场景/道具实体"
        text = "、".join(
            f"{it.get('name')}[{it.get('type')}:{it.get('id')}"
            f"{'' if it.get('has_image') else ',未出图'}]"
            for it in items[:40]
        )
        return f"项目实体（共 {d.get('matched_total', len(items))} 个）：{text}"

    if tool_name == "list_project_shots":
        items = d.get("items") or []
        if not items:
            return "未匹配到镜头"
        text = "、".join(
            f"第{it.get('episode')}集/{it.get('scene_title')}/镜{it.get('seq')}"
            f"[{it.get('shot_id')}"
            f"{',有分镜图' if it.get('has_keyframe') else ',无分镜图'}"
            f"{',已确认' if it.get('keyframe_confirmed') else ''}"
            f"{',有视频' if it.get('has_clip') else ''}]"
            for it in items[:20]
        )
        suffix = f"（仅列前 20 条，共 {len(items)} 条）" if len(items) > 20 else ""
        return f"镜头清单：{text}{suffix}"

    if tool_name == "get_parse_status":
        parts = [
            f"剧本{'已保存 v' + str(d.get('script_version')) if d.get('has_script') else '未保存'}",
            f"解析版本 {d.get('parse_version') if d.get('parse_version') else '无'}",
            f"镜头 {d.get('shot_count', 0)} 个",
            f"合成记录 {d.get('composition_count', 0)} 条",
        ]
        records = d.get("episode_parse_records") or []
        if records:
            bad = [r for r in records if r.get("status") not in ("succeeded", "done")]
            if bad:
                parts.append(
                    "解析未完成明细："
                    + "、".join(
                        f"第{r['episode']}集{r['asset_type']}({r['status']})" for r in bad[:8]
                    )
                )
        return "解析状态：" + "；".join(parts)

    if tool_name == "list_compositions":
        items = d.get("items") or []
        if not items:
            return "暂无合成记录"
        return "合成记录（新→旧）：" + "、".join(
            f"{c.get('composition_id')}(第{c.get('episode')}集,{c.get('status')}"
            f"{',已过期' if c.get('stale') else ''})"
            for c in items[:5]
        )

    if tool_name == "list_exports":
        items = d.get("items") or []
        if not items:
            return "暂无导出记录"
        return "导出记录（新→旧）：" + "、".join(
            f"{e.get('export_id')}({e.get('delivery_profile')},{e.get('status')}"
            f"{',' + str(e.get('size')) + 'B' if e.get('size') else ''})"
            for e in items[:5]
        )

    if tool_name == "list_jobs":
        items = d.get("items") or []
        if not items:
            return "暂无匹配任务"
        return "任务明细：" + "、".join(
            f"{j.get('id')}({j.get('job_type')},{j.get('status')}"
            f"{',' + str(j.get('error_code')) if j.get('error_code') else ''})"
            for j in items[:5]
        )

    if tool_name == "get_job":
        return (
            f"任务 {d.get('id')}：{d.get('job_type')} {d.get('status')}，"
            f"错误 {_short(d.get('error_msg'), 120) if d.get('error_msg') else '无'}，"
            f"尝试 {d.get('attempts')} 次，可重试={d.get('retryable')}"
        )

    if tool_name == "list_asset_versions":
        versions = d.get("versions") or []
        return f"资产 {d.get('asset_id')}（{d.get('name')}）版本：" + "、".join(
            f"v{v.get('version')}({v.get('validity')}"
            f"{',锁定' if v.get('locked') else ''})"
            for v in versions[:8]
        )

    if tool_name == "get_shot_impact":
        return (
            f"镜头 {d.get('shot_id')}（第{d.get('episode')}集/{d.get('scene_title')}/镜{d.get('seq')}）："
            f"确认={d.get('keyframe_confirmed')}，已有产物类型={d.get('current_asset_types') or '无'}"
        )
    return ""


