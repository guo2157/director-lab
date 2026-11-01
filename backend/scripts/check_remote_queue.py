"""只读核对：ComfyUI 远端队列 / 本地任务表按 prompt_id 比对。"""
import json
import sqlite3
import urllib.request

BASE = "https://u1108211-793581244925.westd.seetacloud.com:8443"

raw = urllib.request.urlopen(BASE + "/queue", timeout=20).read()
q = json.loads(raw)
run = [{"num": x[0], "prompt_id": x[1]} for x in q.get("queue_running", [])]
pend = [{"num": x[0], "prompt_id": x[1]} for x in q.get("queue_pending", [])]
print("RUNNING:", json.dumps(run, ensure_ascii=False))
print("PENDING:", json.dumps(pend, ensure_ascii=False))
print("PENDING_COUNT:", len(pend))

ids = [x[1] for x in q.get("queue_running", [])] + [x[1] for x in q.get("queue_pending", [])]
con = sqlite3.connect("data/director_lab.db")
con.row_factory = sqlite3.Row
print("--- prompt_id 匹配 ---")
for pid in ids:
    rows = con.execute(
        "select id,job_type,status,shot_id,created_at,finished_at from job where prompt_id=?",
        (pid,),
    ).fetchall()
    print(pid, "->", [dict(r) for r in rows] or "NO_LOCAL_MATCH")

print("--- 本地在途任务 ---")
for r in con.execute("select id,status,job_type,shot_id,prompt_id from job where status in ('running','queued','submission_unknown')"):
    print(dict(r))

print("--- 最近被取消的 generate_clip ---")
for r in con.execute("select id,status,shot_id,prompt_id,finished_at from job where status='cancelled' and job_type='generate_clip' order by finished_at desc limit 25"):
    print(dict(r))
