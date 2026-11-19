"""SSE 事件流回归测试：连接不得跨越 yield 挂起点。

背景（2026-11 全站超时事故）：
原实现把 `yield` 写在 `try` 内，`finally: db.close()` 要等生成器再次推进
才执行。每条 SSE 在 yield 期间持续占住一个 DB 连接，池默认 5+10=15，
单连接最长 300s，开 15 个任务页即耗尽全站连接池。

本测试锁定三条不变量：
1. 每次轮询结束后（任何 yield 之前）连接已归还
2. 无论流被消费多少事件，并发占用的连接数恒为 1
3. 阻塞查询不跑在事件循环线程上
"""
import asyncio
import threading

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Base
from app.services.event_stream import sse_event_stream


class _ConnTracker:
    """记录连接池的检出/归还，用于断言并发占用峰值。"""

    def __init__(self, engine):
        self.engine = engine
        self.checked_out = 0
        self.peak = 0
        self.closed_while_streaming = 0

    def attach(self):
        from sqlalchemy import event as sa_event

        @sa_event.listens_for(self.engine, "checkout")
        def _out(*_a):
            self.checked_out += 1
            self.peak = max(self.peak, self.checked_out)

        @sa_event.listens_for(self.engine, "checkin")
        def _in(*_a):
            self.checked_out -= 1

        return self


@pytest.fixture()
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'sse.db'}")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def patched_session(engine, monkeypatch):
    """把 SessionLocal 指向测试库，模拟测试套件的常规做法。"""
    import app.core.database as db_mod

    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod, "SessionLocal", sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    )
    return engine


async def _take(agen, n):
    """从异步生成器取 n 个 chunk 后主动关闭（模拟客户端断开）。"""
    out = []
    try:
        for _ in range(n):
            out.append(await agen.__anext__())
    finally:
        await agen.aclose()
    return out


def test_no_connection_held_while_suspended(patched_session):
    """流在 yield 挂起期间不得持有连接。

    这是事故的真正机制：不是单条流占用多个连接，而是每条流在两次轮询之间
    的整个挂起期（客户端空闲等待，最长 300s）都占着 1 个连接。
    池默认 5+10=15，15 条并发流即全站耗尽。

    断言点选在「已取到事件、生成器挂起等待下一次推进」的瞬间：
    修复后应为 0（连接已归还），旧实现为 1（finally 未执行）。
    """
    from sqlalchemy import text

    tracker = _ConnTracker(patched_session).attach()
    held_during_suspend = []

    def fetch(db, seq):
        db.execute(text("SELECT 1")).scalar()
        return [{"seq": seq + 1, "event_type": "test"}]

    async def run():
        gen = sse_event_stream(fetch, last_seq=0, poll_interval=0.01, max_seconds=0.05)
        # 取到一条事件后，生成器正处于挂起状态
        await gen.__anext__()
        held_during_suspend.append(tracker.checked_out)
        await gen.__anext__()
        held_during_suspend.append(tracker.checked_out)
        await gen.aclose()

    asyncio.run(run())

    assert held_during_suspend == [0, 0], (
        f"流挂起期间仍持有连接: {held_during_suspend}（每条 SSE 占 1 个连接，"
        f"15 条并发即耗尽池）"
    )
    assert tracker.checked_out == 0, "流结束后仍有连接未归还"


def test_many_concurrent_streams_do_not_exhaust_pool(patched_session):
    """15 条并发流挂起时，不得占满连接池（事故的完整复现条件）。"""
    from sqlalchemy import text

    tracker = _ConnTracker(patched_session).attach()
    stream_count = 15  # SQLAlchemy 默认 pool_size=5 + max_overflow=10

    def fetch(db, seq):
        db.execute(text("SELECT 1")).scalar()
        return [{"seq": seq + 1, "event_type": "test"}]

    async def run():
        gens = [
            sse_event_stream(fetch, last_seq=0, poll_interval=0.01, max_seconds=0.05)
            for _ in range(stream_count)
        ]
        # 全部推进到挂起状态（模拟 15 个任务页同时打开并空闲）
        for g in gens:
            await g.__anext__()
        peak_while_idle = tracker.checked_out
        for g in gens:
            await g.aclose()
        return peak_while_idle

    peak_while_idle = asyncio.run(run())

    assert peak_while_idle == 0, (
        f"{stream_count} 条空闲流占用 {peak_while_idle} 个连接；"
        f"池上限 15，达到即全站接口超时"
    )


def test_no_connection_leak_on_client_disconnect(patched_session):
    """客户端中途断开（GeneratorExit）后不得遗留连接。"""
    tracker = _ConnTracker(patched_session).attach()

    def fetch(db, seq):
        return [{"seq": seq + 1, "event_type": "test"}]

    async def run():
        gen = sse_event_stream(fetch, last_seq=0, poll_interval=0.01, max_seconds=10)
        await _take(gen, 2)  # 只取 2 个 chunk 就关闭

    asyncio.run(run())
    assert tracker.checked_out == 0, "断开后连接泄漏"


def test_fetch_runs_off_event_loop_thread(patched_session):
    """阻塞查询必须跑在事件循环之外的线程，否则会卡死整个 uvicorn。"""
    seen: list[str] = []

    def fetch(db, seq):
        seen.append(threading.current_thread().name)
        return []

    async def run():
        loop_thread = threading.current_thread().name
        gen = sse_event_stream(fetch, last_seq=0, poll_interval=0.01, max_seconds=0.02)
        await _take(gen, 2)
        return loop_thread

    loop_thread = asyncio.run(run())
    assert seen, "fetch 未被调用"
    assert all(name != loop_thread for name in seen), (
        f"fetch 跑在事件循环线程 {loop_thread} 上，会阻塞全站"
    )


def test_fetch_error_does_not_kill_stream(patched_session):
    """单次查询异常应继续轮询，而不是让整条流中断。"""

    class Boom(Exception):
        pass

    calls = {"n": 0}

    def fetch(db, seq):
        calls["n"] += 1
        if calls["n"] == 1:
            raise Boom("transient db error")
        return [{"seq": 1, "event_type": "ok"}]

    async def run():
        gen = sse_event_stream(fetch, last_seq=0, poll_interval=0.01, max_seconds=0.1)
        return await _take(gen, 3)

    chunks = asyncio.run(run())
    assert calls["n"] >= 2, "异常后未继续轮询"
    assert any("ok" in c for c in chunks), "恢复后未推送事件"


def test_seq_is_monotonic_and_deduplicated(patched_session):
    """seq 必须单调不回退；按契约 fetch 自行过滤 seq，流侧不得重复推送。"""
    # 模拟 outbox：事件表固定两条，fetch 按 seq 严格大于过滤（与真实实现一致）
    outbox = [{"seq": 1, "event_type": "a"}, {"seq": 2, "event_type": "b"}]

    def fetch(db, seq):
        return [e for e in outbox if e["seq"] > seq]

    async def run():
        gen = sse_event_stream(fetch, last_seq=0, poll_interval=0.01, max_seconds=0.05)
        return await _take(gen, 6)

    chunks = asyncio.run(run())
    ids = [int(c.split("id: ")[1].split("\n")[0]) for c in chunks if c.startswith("id: ")]
    assert ids == [1, 2], f"推送序列异常: {ids}"
    assert ids == sorted(ids), "seq 出现回退"


def test_resumes_from_last_seq(patched_session):
    """断线重连传 last_seq 时，只推送严格大于它的事件。"""
    outbox = [{"seq": i, "event_type": "e"} for i in range(1, 6)]

    def fetch(db, seq):
        return [e for e in outbox if e["seq"] > seq]

    async def run():
        gen = sse_event_stream(fetch, last_seq=3, poll_interval=0.01, max_seconds=0.03)
        return await _take(gen, 4)

    chunks = asyncio.run(run())
    ids = [int(c.split("id: ")[1].split("\n")[0]) for c in chunks if c.startswith("id: ")]
    assert ids == [4, 5], f"重连未按 last_seq 续传: {ids}"
