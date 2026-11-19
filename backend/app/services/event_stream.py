"""SSE 事件流通用实现（/api/events、/api/tasks/events、/api/workspace/events 共用）。

历史缺陷（2026-11 定位，三个端点同源）：

1. `yield` 写在 `try` 内，`finally: db.close()` 要等生成器**再次推进**才执行。
   于是每条 SSE 在 yield 期间持续占住一个 DB 连接；连接池默认 5+10=15，
   SSE 单连接最长 300s，开 15 个任务页即可耗尽全站连接池，
   表现为 uvicorn 积压几十个 LISTEN、进程 CPU 极低但全站接口超时。
   py-spy 栈：QueuePool._do_get ← event_stream.<locals>.gen (events.py:54)

2. 阻塞的 SQLAlchemy 查询直接在事件循环里执行。SQLite `busy_timeout=30000`，
   一旦 worker 持写锁，查询会把**整个 uvicorn 事件循环**卡住最多 30s，
   这不是「慢」而是全站无响应。

当前实现：
- 查询在 `asyncio.to_thread` 中执行，不阻塞事件循环
- 结果先物化为纯 dict，Session 在**任何 yield 之前**关闭，连接不跨越挂起点
- 客户端断开（GeneratorExit）时立即停止，不遗留会话
- 查询异常按指数退避的节流日志记录并继续轮询，不中断流、不刷日志

禁止事项：
- 禁止在 yield 之后才关闭 Session
- 禁止在事件循环线程内直接执行阻塞 DB 调用
"""
import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable

from sqlalchemy.orm import Session

from app.core import database as db_module

logger = logging.getLogger("director-lab.sse")

# 轮询间隔（秒）与单连接最长持续时间（秒，防止连接无限挂起）
POLL_INTERVAL = 1.0
MAX_STREAM_SECONDS = 300.0

# 连续失败超过该次数后停止本连接，交由前端重连（避免坏连接空转）
_MAX_CONSECUTIVE_FAILURES = 30

# 事件批次上限
MAX_BATCH = 200

FetchFn = Callable[[Session, int], list[dict]]


def _poll_once(fetch: FetchFn, seq: int) -> list[dict]:
    """在独立线程中执行一次增量查询，返回已物化的事件列表。

    关键点：Session 在函数返回前关闭，且返回的是纯 dict，
    调用方拿到结果时连接已归还连接池，不会跨越 yield 挂起点。

    SessionLocal 在调用时从 database 模块解析，而非模块导入时绑定：
    测试通过 monkeypatch 替换会话工厂后，轮询必须跟随替换，
    否则会静默连到生产库。
    """
    db = db_module.SessionLocal()
    try:
        return fetch(db, seq)
    finally:
        db.close()


async def sse_event_stream(
    fetch: FetchFn,
    *,
    last_seq: int = 0,
    poll_interval: float = POLL_INTERVAL,
    max_seconds: float = MAX_STREAM_SECONDS,
) -> AsyncIterator[str]:
    """通用 outbox 增量事件流。

    `fetch(db, seq)` 负责按 seq 严格大于读取一批事件并返回 dict 列表
    （每个 dict 必须含 `seq` 字段）。本函数只负责调度、序列化与心跳。
    """
    seq = last_seq
    elapsed = 0.0
    failures = 0

    while elapsed < max_seconds:
        try:
            # 阻塞查询丢到线程池，事件循环保持可响应
            rows = await asyncio.to_thread(_poll_once, fetch, seq)
            failures = 0
        except Exception:
            failures += 1
            # 节流：只记首次与每 10 次，避免 DB 故障时刷爆日志
            if failures == 1 or failures % 10 == 0:
                logger.warning(
                    "sse poll failed (consecutive=%d), stream continues", failures, exc_info=True
                )
            if failures >= _MAX_CONSECUTIVE_FAILURES:
                logger.error("sse poll failed %d times consecutively, closing stream", failures)
                return
            rows = []

        for data in rows:
            try:
                ev_seq = int(data["seq"])
            except (KeyError, TypeError, ValueError):
                logger.warning("sse event missing usable seq, skipped: %r", data)
                continue
            seq = max(seq, ev_seq)
            yield f"id: {ev_seq}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"

        # 心跳注释，防代理断连
        yield ": ping\n\n"
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval


def sse_response(iterator: AsyncIterator[str]):
    """统一 SSE 响应头，避免三个端点各写一份。"""
    from fastapi.responses import StreamingResponse

    return StreamingResponse(
        iterator,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
