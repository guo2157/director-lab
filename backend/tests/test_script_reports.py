"""P1-9 regression: three independent reports (quality / integrity / feasibility).

- to_reports_dict splits evaluate_script result into three reports while
  keeping original top-level fields intact for backward compatibility.
- Report status logic: quality (score + severity), integrity (coverage + gate),
  feasibility (risk count + dimensions).
- Null/skipped degradation: eval=None returns status=skipped for quality/feasibility,
  integrity still runs on coverage + gate data.
"""
from app.services.report_bundle import (
    build_feasibility_report,
    build_integrity_report,
    build_quality_report,
    to_reports_dict,
)


def test_quality_report_passed():
    r = build_quality_report({
        "overall_score": 8,
        "issues": [{"severity": "minor", "detail": "minor issue"}],
        "strengths": [{"detail": "good"}],
        "suggestions": [],
        "plot_holes": [],
        "summary": "great script",
    })
    assert r["status"] == "passed"
    assert r["items"]["overall_score"] == 8
    assert r["items"]["severity"]["minor"] == 1


def test_quality_report_critical():
    r = build_quality_report({
        "overall_score": 3,
        "issues": [{"severity": "critical", "detail": "bad"}],
        "strengths": [],
        "suggestions": [],
        "plot_holes": [],
        "summary": "bad",
    })
    assert r["status"] == "failed"


def test_feasibility_report_empty():
    r = build_feasibility_report({
        "production_risks": [],
        "character_convergence": {},
        "scene_convergence": {},
        "shot_feasibility": {},
        "dialogue_feasibility": {},
        "prop_risks": {},
    })
    assert r["status"] == "passed"
    assert r["items"]["total_alerts"] == 0


def test_feasibility_report_with_risks():
    r = build_feasibility_report({
        "production_risks": [
            {"severity": "critical", "detail": "fire scene"},
            {"severity": "major", "detail": "crowd"},
        ],
        "character_convergence": {
            "alias_conflicts": [{"name": "a", "conflict": "b"}],
        },
        "scene_convergence": {},
        "shot_feasibility": {
            "crowd_or_complex": ["complex shot"],
        },
        "dialogue_feasibility": {},
        "prop_risks": {},
    })
    assert r["status"] == "failed"
    assert r["items"]["total_alerts"] >= 3


def test_integrity_report_passed():
    r = build_integrity_report(
        {"overall": "passed", "episodes": {"source": 5, "parsed": 5, "missing": []},
         "scenes": {"source": 10, "parsed": 10},
         "dialogues": {"source": 30, "matched": 30},
         "characters": {"source": 4, "matched": 4}, "props": {"source": 2, "matched": 2}},
        {"completion_status": "complete", "failed_batches": 0},
        None,
    )
    assert r["status"] == "passed"


def test_integrity_report_failed_on_missing_eps():
    r = build_integrity_report(
        {"overall": "passed", "episodes": {"source": 5, "parsed": 4, "missing": [5]},
         "scenes": {"source": 10, "parsed": 10},
         "dialogues": {"source": 30, "matched": 30},
         "characters": {"source": 4, "matched": 4}, "props": {"source": 2, "matched": 2}},
        {"completion_status": "partial", "failed_batches": 0},
        None,
    )
    assert r["status"] == "failed"


def test_integrity_report_blocked_by_gate():
    r = build_integrity_report(
        {"overall": "passed", "episodes": {"source": 5, "parsed": 5, "missing": []},
         "scenes": {"source": 10, "parsed": 10},
         "dialogues": {"source": 30, "matched": 30},
         "characters": {"source": 4, "matched": 4}, "props": {"source": 2, "matched": 2}},
        {"completion_status": "complete", "failed_batches": 0},
        {"overall": "BLOCKED"},
    )
    assert r["status"] == "failed"


def test_to_reports_dict_full():
    eval_result = {
        "overall_score": 7,
        "issues": [{"severity": "major", "detail": "pacing"}],
        "strengths": [{"detail": "good chars"}],
        "suggestions": [],
        "plot_holes": [],
        "production_risks": [{"severity": "major", "detail": "stunts"}],
        "summary": "ok",
        "character_convergence": {},
        "scene_convergence": {},
        "shot_feasibility": {},
        "dialogue_feasibility": {},
        "prop_risks": {},
    }
    cov = {"overall": "passed", "episodes": {"source": 3, "parsed": 3, "missing": []},
            "scenes": {"source": 5, "parsed": 5},
            "dialogues": {"source": 15, "matched": 15},
            "characters": {"source": 2, "matched": 2}, "props": {"source": 1, "matched": 1}}
    meta = {"completion_status": "complete", "failed_batches": 0}
    reports = to_reports_dict(eval_result, cov, meta, None)
    assert set(reports.keys()) == {"quality_report", "feasibility_report", "integrity_report"}
    # score=7 + 1 major → passed（逻辑：n_maj>2 或 score<5 才 partial）
    assert reports["quality_report"]["status"] == "passed"
    assert reports["integrity_report"]["status"] == "passed"
    # 1 major risk + 0 detail items → total=1 ≤ 5 → passed
    assert reports["feasibility_report"]["status"] == "passed"


def test_to_reports_dict_skipped():
    reports = to_reports_dict(None, None, None, None)
    assert reports["quality_report"]["status"] == "skipped"
    assert reports["feasibility_report"]["status"] == "skipped"
    assert "integrity_report" in reports
