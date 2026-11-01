#!/usr/bin/env python3
"""导演台 mock 全流程测评：小马过河。

流程链：新建项目 → 保存剧本 → 分析剧本 → 资产生成（角色/场景/道具图）
→ 人工审核资产 → 分镜图 → 人工审核分镜 → 分镜看板 → 批量生成看板视频
→ 审核 → 合成导出视频。

- 渲染走 mock（服务端 DLAB_RENDER_MOCK=true），解析走真实 LLM（deepseek）
- 每步执行后输出 pipeline 状态（节点 + 阶段 + 下一步指引）
- 输出 JSON trace（stdout），供测评报告

运行：cd backend && .venv/bin/python scripts/mock_flow_test.py
"""
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

BASE = "http://127.0.0.1:8000"
POLL_INTERVAL = 2.0
POLL_TIMEOUT = 300.0

SCRIPT_TEXT = """《小马过河》

第一场：马棚里，清晨。小马帮妈妈驮半口袋麦子去磨坊。妈妈叮嘱它路上小心。

第二场：小路上。小马驮着麦子走到一条小河边，河水挡住了去路。老牛正在河边吃草，小马问：牛伯伯，这条河我能蹚过去吗？老牛说：水很浅，刚没过小腿，能蹚过去。小马正要过河，松鼠从树上跳下来大喊：小马别过河，水很深，昨天我的伙伴就是在这条河里淹死的！

第三场：河边。小马没了主意，跑回家问妈妈。妈妈说：那条河到底是深是浅，你亲自去试一试就知道了。小马回到河边，小心地蹚到对岸，原来河水既不像老牛说的那样浅，也不像松鼠说的那样深。
"""


def api(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"HTTP {e.code} {method} {path}: {err_body}") from e


def pipeline_status(project_id: str) -> dict:
    from app.core.database import SessionLocal
    from app.models import Project
    from app.services.pipeline_state import get_pipeline_status
    db = SessionLocal()
    try:
        p = db.get(Project, project_id)
        if p is None:
            return {"error": "project_not_found"}
        st = get_pipeline_status(db, project_id)
        return {
            "node_key": st.node_key,
            "node_name": st.node_name,
            "stage": st.stage,
            "next_hint": st.next_hint,
        }
    finally:
        db.close()


def wait_job(job_id: str) -> dict:
    deadline = time.monotonic() + POLL_TIMEOUT
    while time.monotonic() < deadline:
        j = api("GET", f"/api/jobs/{job_id}")
        if j.get("status") in ("succeeded", "failed", "cancelled"):
            return j
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"任务 {job_id} 超时未完成")


def wait_jobs(job_ids: list[str]) -> list[dict]:
    return [wait_job(j) for j in job_ids]


trace: dict = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S"), "steps": []}


def step(step_id: str, name: str, fn):
    print(f"\n=== [{step_id}] {name} ===", flush=True)
    rec = {"step": step_id, "name": name}
    t0 = time.monotonic()
    try:
        rec["result"] = fn()
        rec["ok"] = True
    except Exception as e:
        rec["ok"] = False
        rec["error"] = str(e)
        trace["steps"].append(rec)
        print(f"FAILED: {e}", flush=True)
        _finish()
        raise
    rec["duration_ms"] = int((time.monotonic() - t0) * 1000)
    trace["steps"].append(rec)
    print(json.dumps(rec.get("result"), ensure_ascii=False)[:400], flush=True)
    return rec


def _finish():
    trace["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    print("\n===== TRACE =====", flush=True)
    print(json.dumps(trace, ensure_ascii=False, indent=2), flush=True)


STATE = {}


def main() -> None:
    # 1. 新建项目
    def s1():
        p = api("POST", "/api/projects", {"name": "小马过河-mock测评", "aspect_ratio": "16:9"})
        STATE["pid"] = p["id"]
        return {"project_id": p["id"], "pipeline": pipeline_status(p["id"])}

    step("01", "新建项目", s1)
    pid = STATE["pid"]

    # 2. 保存剧本
    def s2():
        sv = api("POST", f"/api/projects/{pid}/script-versions", {"text": SCRIPT_TEXT})
        STATE["sv_id"] = sv["script_version_id"]
        return {"script_version_id": sv["script_version_id"], "version": sv["version"], "pipeline": pipeline_status(pid)}

    step("02", "保存剧本", s2)

    # 2.5 剧本评估 + 确认（推进 pipeline 阶段：estimate → select_range）
    def s2b():
        est = api("POST", f"/api/projects/{pid}/estimate", {
            "script_version_id": STATE["sv_id"], "text": SCRIPT_TEXT,
        })
        c = api("POST", f"/api/projects/{pid}/estimates/{est['estimate_id']}/confirm", {
            "script_version_id": STATE["sv_id"],
            "episode_start": 1, "episode_count": 1,
        })
        return {"estimate_id": est["estimate_id"], "confirm": c, "pipeline": pipeline_status(pid)}

    step("02b", "剧本评估 + 确认", s2b)

    # 3. 分析剧本（真实 LLM 解析）
    def s3():
        r = api("POST", f"/api/projects/{pid}/parse", {
            "script_version_id": STATE["sv_id"], "text": SCRIPT_TEXT,
            "episode_start": 1, "episode_count": 1,
        })
        j = wait_job(r["job_id"])
        if j["status"] != "succeeded":
            raise RuntimeError(f"解析任务失败: {j.get('error_code')} {j.get('error_message')}")
        pv_id = j["result"]["parse_version_id"]
        STATE["pv_id"] = pv_id
        c = api("POST", f"/api/projects/{pid}/parse-versions/{pv_id}/confirm", {"current_parse_version_id": None})
        return {"parse_version_id": pv_id, "confirm": c, "pipeline": pipeline_status(pid)}

    step("03", "分析剧本（LLM 解析 + 确认）", s3)

    # 4. 资产生成：角色 → 场景 → 道具
    def s4():
        out = {}
        for plan_type in ("character", "scene", "prop"):
            r = api("POST", f"/api/projects/{pid}/asset-plans/generate", {"plan_type": plan_type})
            jobs = wait_jobs([x["job_id"] for x in r["jobs"]])
            bad = [x for x in jobs if x["status"] != "succeeded"]
            if bad:
                raise RuntimeError(f"{plan_type} 生成失败: {[(x.get('error_code'), x.get('error_message')) for x in bad]}")
            out[plan_type] = {"count": r["count"], "jobs": len(jobs)}
        out["pipeline"] = pipeline_status(pid)
        return out

    step("04", "资产生成（角色/场景/道具图，mock 渲染）", s4)

    # 5. 人工审核资产
    def s5():
        r = api("POST", f"/api/projects/{pid}/assets-review", {"action": "approve"})
        return {"review": r.get("assets_review_status", r), "pipeline": pipeline_status(pid)}

    step("05", "人工审核资产（approve）", s5)

    # 6. 分镜图生成
    def s6():
        r = api("POST", f"/api/projects/{pid}/asset-plans/generate", {"plan_type": "keyframe"})
        jobs = wait_jobs([x["job_id"] for x in r["jobs"]])
        bad = [x for x in jobs if x["status"] != "succeeded"]
        if bad:
            raise RuntimeError(f"分镜图生成失败: {[(x.get('error_code'), x.get('error_message')) for x in bad]}")
        STATE["keyframe_count"] = r["count"]
        return {"count": r["count"], "skipped": r["skipped"], "pipeline": pipeline_status(pid)}

    step("06", "分镜图生成（mock 渲染）", s6)

    # 7. 人工审核分镜（批量确认）
    def s7():
        r = api("POST", f"/api/projects/{pid}/keyframe/confirm", {})
        return {"confirmed_count": r["confirmed_count"], "skipped_count": r["skipped_count"], "pipeline": pipeline_status(pid)}

    step("07", "人工审核分镜（批量确认）", s7)

    # 8. 分镜看板（查询镜头与分镜图清单）
    def s8():
        shots = api("GET", f"/api/projects/{pid}/shots?episode=1")
        items = shots.get("items", shots) if isinstance(shots, dict) else shots
        board = [
            {"shot_id": s["id"], "episode": s.get("episode"), "seq": s.get("seq"),
             "kind": s.get("shot_kind"), "confirmed": s.get("keyframe_confirmed")}
            for s in items
        ]
        return {"shot_count": len(board), "board": board, "pipeline": pipeline_status(pid)}

    step("08", "分镜看板", s8)

    # 9. 批量生成看板视频（clip）
    def s9():
        r = api("POST", f"/api/projects/{pid}/asset-plans/generate", {"plan_type": "clip"})
        jobs = wait_jobs([x["job_id"] for x in r["jobs"]])
        bad = [x for x in jobs if x["status"] != "succeeded"]
        if bad:
            raise RuntimeError(f"看板视频生成失败: {[(x.get('error_code'), x.get('error_message')) for x in bad]}")
        return {"count": r["count"], "skipped": r["skipped"], "pipeline": pipeline_status(pid)}

    step("09", "批量生成看板视频（mock 渲染）", s9)

    # 9.5 生成对白/旁白镜头音频（tts，镜头状态转 done 的必要条件）
    def s9b():
        shots = api("GET", f"/api/projects/{pid}/shots?episode=1")
        items = shots.get("items", shots) if isinstance(shots, dict) else shots
        need_audio = [s for s in items if s.get("shot_kind") in ("dialogue", "narration")]
        job_ids = []
        for s in need_audio:
            r = api("POST", f"/api/shots/{s['id']}/generate", {"target": "audio"})
            jid = r.get("id") or r.get("job", {}).get("id")
            job_ids.append(jid)
        jobs = wait_jobs(job_ids)
        bad = [x for x in jobs if x["status"] != "succeeded"]
        if bad:
            raise RuntimeError(f"音频生成失败: {[(x.get('error_code'), x.get('error_message')) for x in bad]}")
        return {"audio_jobs": len(job_ids), "pipeline": pipeline_status(pid)}

    step("09b", "生成对白/旁白音频（tts，mock 渲染）", s9b)

    # 10. 审核（检查镜头状态，处理 review 镜头）
    def s10():
        shots = api("GET", f"/api/projects/{pid}/shots?episode=1")
        items = shots.get("items", shots) if isinstance(shots, dict) else shots
        by_status = {}
        for s in items:
            by_status[s.get("status")] = by_status.get(s.get("status"), 0) + 1
        return {"shot_status": by_status, "pipeline": pipeline_status(pid)}

    step("10", "视频审核（镜头状态检查）", s10)

    # 11. 合成
    def s11():
        r = api("POST", f"/api/projects/{pid}/compose", {"include_subtitle": True, "subtitle_mode": "burn"})
        j = wait_job(r["job_id"])
        if j["status"] != "succeeded":
            raise RuntimeError(f"合成失败: {j.get('error_code')} {j.get('error_message')}")
        comps = api("GET", f"/api/projects/{pid}/compositions")
        items = comps.get("items", comps) if isinstance(comps, dict) else comps
        comp = [c for c in items if c.get("status") == "done"]
        if not comp:
            raise RuntimeError(f"无 done 状态合成版本: {items}")
        STATE["comp_id"] = comp[0]["composition_id"]
        return {"composition_id": comp[0]["composition_id"], "file_path": comp[0].get("file_path"), "pipeline": pipeline_status(pid)}

    step("11", "合成成片", s11)

    # 12. 导出
    def s12():
        r = api("POST", f"/api/projects/{pid}/export", {
            "composition_id": STATE["comp_id"], "delivery_profile": "standard",
        })
        j = wait_job(r["job_id"])
        if j["status"] != "succeeded":
            raise RuntimeError(f"导出失败: {j.get('error_code')} {j.get('error_message')}")
        exps = api("GET", f"/api/projects/{pid}/exports")
        items = exps.get("items", exps) if isinstance(exps, dict) else exps
        exp = items[-1] if items else {}
        return {"export": exp, "pipeline": pipeline_status(pid)}

    step("12", "导出成片", s12)

    _finish()
    print("\n全流程测评完成", flush=True)


if __name__ == "__main__":
    main()
