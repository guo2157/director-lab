"""任务协作式取消：跨进程取消信号 + 立即中断在途流。

为什么需要它
------------
``POST /api/jobs/{id}/cancel`` 原本只把数据库里的 status 改成 ``cancelled``，
但 script_agent 的执行线程此刻正阻塞在 ``httpx`` 的 socket read 上
（LLM 单次调用实测可达 24 分钟）。线程感知不到数据库变化，会一直跑到
自然结束，最后把 status 覆写成 ``succeeded`` —— 用户点了「停止解析」，
解析却继续烧 token 并且结果照样落库。

为什么不能只用进程内 set（重要教训）
----------------------------------
本模块最初实现为纯内存 ``set``。实测发现 backend（uvicorn）与 worker
（``python -m app.worker.scheduler``）是**两个独立进程**，set 完全不共享：
点取消后 API 侧登记成功、数据库 status 变成 cancelled，但 worker 线程毫无
感知，实测取消后仍写出 400+ 条 ``agent.llm_delta``，还继续跑评估与分块。

所以唯一可靠的跨进程通道是数据库 —— 但 ``is_cancelled`` 要跑在每个 SSE 行上
（一秒可达数千次），每次查库不可接受。折中：**带节流的 DB 轮询 + 结果缓存**。
同一个 job 每 ``_POLL_INTERVAL`` 秒最多查一次库，其间复用缓存值。

为什么还要「中断回调」（register_abort）
--------------------------------------
轮询只能在下一条 SSE 行到达时才被检查。供应商可能几十秒不发数据
（实测网关有「连接不断但不吐字」的情况），此时线程阻塞在 socket read 上，
光靠轮询要等到空闲超时（120s）才退出，token 已经继续计费。
``register_abort`` 让 chat_stream 登记「关闭本响应」的回调：同进程取消时
直接掐断连接，读取立刻抛错返回 —— 这才是真正的「释放 LLM 资源」。
跨进程场景下由 worker 侧轮询到 cancelled 后自行关闭（见 chat_stream）。

进程内 set 仍然保留：同进程部署（API 与 worker 合一）时可零延迟命中，
且能让「任务开始前就被取消」立即生效。
"""

from __future__ import annotations

import threading
import time

_LOCK = threading.Lock()
_CANCELLED: set[str] = set()
#: job_id → (上次查库的 monotonic 时间, 上次查到的取消状态)
_CACHE: dict[str, tuple[float, bool]] = {}
#: job_id → 中断回调列表（通常是关闭正在读取的 HTTP 响应）
_ABORTS: dict[str, list] = {}

# 查库间隔：0.5 秒足够让「点停止」在体感上即时生效，又不会给 SQLite 造成压力。
# SSE 行本身是批量到达的，更短的间隔收益有限；但 2s 会让用户觉得「点了没反应」。
_POLL_INTERVAL = 0.5


# ---------- 中断回调（立即释放 LLM 连接） ----------
def register_abort(job_id: str, fn) -> None:
    """登记「立即中断」回调（通常是关闭 httpx 响应对象）。

    为什么需要：``should_cancel`` 只在下一条 SSE 行到达时才被检查，
    供应商不发数据时线程会一直阻塞在 socket read 上，等到空闲超时才退出。
    登记 close 回调后，取消一到达就直接把连接掐掉，读取立刻返回，
    LLM 侧连接与 token 消耗同时释放。
    """
    if not job_id or fn is None:
        return
    with _LOCK:
        _ABORTS.setdefault(job_id, []).append(fn)


def unregister_abort(job_id: str, fn) -> None:
    """注销中断回调（流正常结束后必须清掉，否则会误关下一个请求）。"""
    if not job_id or fn is None:
        return
    with _LOCK:
        fns = _ABORTS.get(job_id)
        if not fns:
            return
        try:
            fns.remove(fn)
        except ValueError:
            pass
        if not fns:
            _ABORTS.pop(job_id, None)


def _fire_aborts(job_id: str) -> None:
    """触发该任务所有已登记的中断回调（取出即清，异常隔离）。"""
    with _LOCK:
        fns = list(_ABORTS.pop(job_id, []) or [])
    for fn in fns:
        try:
            fn()
        except Exception:  # noqa: BLE001
            pass


# ---------- 取消信号 ----------
def request(job_id: str) -> None:
    """登记取消请求。幂等，可重复调用。"""
    if not job_id:
        return
    with _LOCK:
        _CANCELLED.add(job_id)
        _CACHE[job_id] = (time.monotonic(), True)
    # 同进程部署时立刻掐断在途流；跨进程时由 worker 轮询到 cancelled 后自行关闭
    _fire_aborts(job_id)


def is_cancelled(job_id: str) -> bool:
    """查询是否已被请求取消。热路径调用，带节流的跨进程 DB 轮询。

    检查顺序：
    1. 进程内 set —— 命中即返回，零 IO；
    2. 缓存未过期 —— 直接复用上次结论；
    3. 查库 —— 读 ``job.status``，为 cancelled 即视为取消。

    任何异常都返回 False（不取消）：查库失败不能把任务误杀，
    任务自身的 stall 看护会兜住真正卡死的情况。
    """
    if not job_id:
        return False
    with _LOCK:
        if job_id in _CANCELLED:
            return True
        now = time.monotonic()
        cached = _CACHE.get(job_id)
        if cached is not None and now - cached[0] < _POLL_INTERVAL:
            return cached[1]

    # 查库放在锁外：SQLite 查询可能几十毫秒，持锁会阻塞其他解析线程。
    flag = _read_db_status(job_id)
    with _LOCK:
        _CACHE[job_id] = (now, flag)
        if flag:
            _CANCELLED.add(job_id)
    return flag


def _read_db_status(job_id: str) -> bool:
    """从数据库读任务终态是否为 cancelled。"""
    try:
        from app.core.database import SessionLocal
        from app.models import Job

        db = SessionLocal()
        try:
            row = db.query(Job.status).filter(Job.id == job_id).first()
            return bool(row is not None and row[0] == "cancelled")
        finally:
            db.close()
    except Exception:  # noqa: BLE001
        # 库不可用 / 表不存在（单测环境）时一律不取消
        return False


def clear(job_id: str) -> None:
    """任务终态后清理登记与缓存，避免泄漏。

    必须清缓存：否则同一 job_id 复跑（force 重解析可能复用 id）
    会命中上次的 True 缓存被立刻误判取消。
    """
    if not job_id:
        return
    with _LOCK:
        _CANCELLED.discard(job_id)
        _CACHE.pop(job_id, None)
        _ABORTS.pop(job_id, None)


class JobCancelled(Exception):
    """工作线程检测到取消信号时抛出，由执行器捕获并落 cancelled 终态。

    不继承 LLMError：它不是模型/网络故障，不能进重试逻辑，
    否则「取消」会被 call_with_retry 当成可重试错误又跑一轮。
    """

    def __init__(self, job_id: str, reason: str = "用户取消解析") -> None:
        self.job_id = job_id
        self.reason = reason
        super().__init__(f"{reason}（job {job_id}）")
