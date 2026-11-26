"""Agent Runtime 确定性回复渲染（Phase B B8，B6 先使用渲染函数）。

约束：
- 纯函数、确定性、不调用 LLM。
- 禁止出现「已完成」等措辞，除非 lifecycle_state == completed；
  禁止在 waiting_job 时出现「生成完成」。
- 只陈述数据库事实（步骤状态、错误码、审批意见），不编造进度百分比。
"""
from __future__ import annotations

from typing import Any

_FORBIDDEN_UNLESS_COMPLETED = ("已完成", "全部完成", "都完成了")
_FORBIDDEN_IN_WAITING_JOB = ("生成完成", "已生成", "出图完成")


def _goal_label(goal: Any) -> str:
    desc = getattr(goal, "description", "") or ""
    return desc[:40]


def _step_names(plan_steps: list[dict], *, limit: int = 5) -> list[str]:
    names: list[str] = []
    for step in plan_steps or []:
        label = (step.get("description") or step.get("step_key") or step.get("tool_name") or "").strip()
        if label and label not in names:
            names.append(label)
    return names[:limit]


def render_tick_reply(tick_result: Any, *, goal: Any, plan_steps: list[dict]) -> str:
    """按 lifecycle_state 生成助手回复（确定性模板）。"""
    state = getattr(tick_result, "lifecycle_state", None) or getattr(tick_result, "status", "") or "running"
    waiting = getattr(tick_result, "waiting", {}) or {}
    failed = getattr(tick_result, "failed", []) or []
    counts = getattr(tick_result, "counts", {}) or {}
    label = _goal_label(goal)

    if state == "waiting_approval":
        approvals = waiting.get("approval") or []
        n = len(approvals)
        names = _step_names(plan_steps)
        detail = "、".join(names) if names else "相关生成项"
        return (
            f"已为 {n} 项生成申请提交审批，请在审批面板确认（共 {n} 项）：{detail}。"
            "审批通过后会自动继续，不需要保持页面打开。"
        )

    if state == "waiting_job":
        jobs = waiting.get("job") or []
        n = len(jobs)
        return (
            f"已提交 {n} 个生成任务，正在生成，完成后会自动继续。"
            "你可以关闭页面，进度会保存在任务状态里。"
        )

    if state == "waiting_user":
        users = waiting.get("user") or []
        if users:
            lines = []
            for item in users[:5]:
                code = item.get("error_code") or "unknown"
                message = (item.get("message") or "").strip()
                lines.append(f"- {code}：{message}" if message else f"- {code}")
            return "需要你处理以下问题后才能继续：\n" + "\n".join(lines)
        return "需要你先处理前置条件，处理完成后回复「继续」即可。"

    if state == "completed":
        return (
            f"目标「{label}」已完成：成功 {counts.get('succeeded', 0)} 步，"
            f"跳过 {counts.get('skipped', 0)} 步。"
        )

    if state == "partially_completed":
        lines = [
            f"- {item.get('tool_name') or item.get('step_id')}：{item.get('error_code') or '失败'}"
            for item in failed[:5]
        ]
        tail = ("\n失败项：\n" + "\n".join(lines)) if lines else ""
        return (
            f"目标「{label}」部分完成：成功 {counts.get('succeeded', 0)} 步，"
            f"失败 {counts.get('failed', 0)} 步。{tail}"
        )

    if state == "failed":
        lines = [
            f"- {item.get('tool_name') or item.get('step_id')}：{item.get('error_code') or '失败'}"
            for item in failed[:5]
        ]
        tail = ("\n" + "\n".join(lines)) if lines else ""
        return f"目标「{label}」执行失败，失败 {counts.get('failed', 0)} 步。{tail}"

    if state in ("paused", "cancelled"):
        return f"目标「{label}」当前状态为 {state}，没有继续推进。"

    if state == "busy":
        return "该目标正在由其他执行者推进，请稍后查看状态。"

    pending = counts.get("pending", 0)
    return f"目标「{label}」正在推进，还有 {pending} 个步骤待执行。"


def reply_violates_wording(reply: str, lifecycle_state: str) -> bool:
    """测试辅助：检查回复是否出现与状态不符的措辞。"""
    if lifecycle_state != "completed" and any(w in reply for w in _FORBIDDEN_UNLESS_COMPLETED):
        return True
    return lifecycle_state == "waiting_job" and any(
        w in reply for w in _FORBIDDEN_IN_WAITING_JOB
    )
