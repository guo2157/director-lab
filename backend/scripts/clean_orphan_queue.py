"""一次性清理 ComfyUI 孤儿任务（方案一，第二轮）。

实际队列在两次观察间发生了推进：
- 02faa22f（job_6056cc3ae1e6）已自行跑完并被移除
- 24609638（job_3b5373ffc1cd）已从排队变为运行中
- 75636c31（job_25d020cbf027）已由本脚本精确删除
本轮只需中断正在运行的孤儿 24609638，正常任务 a0bf269f 会自动上位。
仅处理上述白名单 prompt_id，不触碰其他任务，不修改本地数据库。
"""
import json

from app.integrations.comfyui import ComfyUIClient

BASE = "https://u1108211-793581244925.westd.seetacloud.com:8443"
ORPHANS = {
    "02faa22f-f9c7-4b6f-aec7-9864bf65cc31",  # job_6056cc3ae1e6（已自行结束）
    "24609638-8a83-4a3c-a485-d8d523aeae41",  # job_3b5373ffc1cd
    "75636c31-dfe7-473e-9473-617297113535",  # job_25d020cbf027（已删除）
}
KEEP = "a0bf269f-4472-4ffc-9e88-9967e30506dc"  # job_2a7a4d487b9a（正常任务）

client = ComfyUIClient(base_url=BASE, timeout=30)
q = client.get_queue()
running = [x[1] for x in q.get("queue_running", [])]
pending = [x[1] for x in q.get("queue_pending", [])]
print("BEFORE_RUNNING:", json.dumps(running, ensure_ascii=False))
print("BEFORE_PENDING:", json.dumps(pending, ensure_ascii=False))

if KEEP not in running and KEEP not in pending:
    print("ABORT: 正常任务既不在运行也不在排队，需人工确认")
    raise SystemExit(1)

# 仍在排队的孤儿优先精确删除，避免下一轮再挡路
to_delete = [p for p in pending if p in ORPHANS]
if to_delete:
    client._request("POST", "/queue", json={"delete": to_delete})
    print("DELETED_PENDING:", to_delete)

# 正在运行的孤儿才中断（全局 interrupt，仅作用于当前执行中的那一个）
run_orphans = [p for p in running if p in ORPHANS]
if run_orphans:
    client._request("POST", "/interrupt")
    print("INTERRUPTED_RUNNING:", run_orphans)
else:
    print("SKIP_INTERRUPT: 当前运行的不是孤儿任务")

q2 = client.get_queue()
print("AFTER_RUNNING:", json.dumps([x[1] for x in q2.get("queue_running", [])], ensure_ascii=False))
print("AFTER_PENDING:", json.dumps([x[1] for x in q2.get("queue_pending", [])], ensure_ascii=False))
print("KEEP_STATE:", "running" if KEEP in [x[1] for x in q2.get("queue_running", [])] else ("pending" if KEEP in [x[1] for x in q2.get("queue_pending", [])] else "missing"))
