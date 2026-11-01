"""只读核对：验证 WS 进度事件是否跨任务串扰，以及孤儿任务耗时。"""
import sqlite3
from datetime import datetime

con = sqlite3.connect("data/director_lab.db")
con.row_factory = sqlite3.Row

for jid in ("job_2a7a4d487b9a", "job_20791ff21176", "job_1bea5ae8fb44"):
    print("===", jid, "===")
    rows = con.execute(
        "select event_type,payload,created_at from job_event where job_id=? "
        "and event_type in ('job.progress','job.started','job.submitted') "
        "order by event_seq",
        (jid,),
    ).fetchall()
    for r in rows:
        print("  ", r["event_type"], str(r["payload"])[:100], r["created_at"])

print("=== 孤儿任务耗时 ===")
rows = con.execute(
    "select id,status,shot_id,prompt_id,created_at,started_at from job "
    "where id in ('job_2a7a4d487b9a','job_942254fd89de','job_1bea5ae8fb44')"
).fetchall()
now = datetime.utcnow()
for r in rows:
    d = dict(r)
    if d.get("started_at"):
        try:
            st = datetime.fromisoformat(d["started_at"])
            d["elapsed_min"] = round((now - st).total_seconds() / 60, 1)
        except Exception:
            d["elapsed_min"] = None
    print("  ", d)

print("=== 当前 UTC ===", now.isoformat())
