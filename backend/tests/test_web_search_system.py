"""联网搜索系统单元测试（联网搜索整改 Phase 2/3）。
覆盖：
- search_validator：URL/标题校验、去重、剔除原因统计
- search_service：状态机流转、日志先行、空结果换词重试、失败留痕（mock 网络依赖）
- web_fetch：SSRF 防护（内网/localhost/非 http 协议拒绝）
"""
from datetime import datetime

import pytest
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.models import Base, SearchRun
from app.services.search_validator import validate_search_results
from app.services import search_service


@pytest.fixture()
def db(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'search_test.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)()
    yield session
    session.close()


# ---------- search_validator ----------

def test_validator_ok():
    results = [
        {"title": "结果一", "url": "https://a.com/1", "snippet": "s"},
        {"title": "结果二", "url": "https://b.com/2", "snippet": ""},
    ]
    v = validate_search_results(results)
    assert v.valid_count == 2
    assert not v.removed_reasons


def test_validator_invalid_url_and_title():
    results = [
        {"title": "无URL", "url": "", "snippet": ""},
        {"title": "", "url": "https://a.com/1", "snippet": ""},
        {"title": "ftp", "url": "ftp://a.com/1", "snippet": ""},
    ]
    v = validate_search_results(results)
    assert v.valid_count == 0
    assert v.removed_reasons.get("invalid_url") == 2
    assert v.removed_reasons.get("missing_title") == 1


def test_validator_dedup():
    results = [
        {"title": "一", "url": "https://a.com/1", "snippet": ""},
        {"title": "重复", "url": "https://a.com/1", "snippet": ""},
    ]
    v = validate_search_results(results)
    assert v.valid_count == 1
    assert v.removed_reasons.get("duplicate_url") == 1


# ---------- search_service（mock 网络） ----------

def _mock_web_search(ok: bool, results: list[dict]):
    def fake(query: str, **kwargs):
        if ok:
            return {
                "status": "success", "query": query, "provider": "bing",
                "result_count": len(results), "results": results, "error": None,
            }
        return {"status": "failed", "query": query, "provider": None,
                "result_count": 0, "results": [], "error": "all failed"}
    return fake


class _FakeFetch:
    def __init__(self, status: str, content: str = "正文" * 30):
        self.status = status
        self.content = content


def test_search_success_flow(db, monkeypatch):
    """搜索成功：状态机走到 success，SearchRun/SearchResult 落库。"""
    results = [
        {"title": f"结果{i}", "url": f"https://a.com/{i}", "snippet": "s", "domain": "a.com"}
        for i in range(3)
    ]
    monkeypatch.setattr(search_service, "web_search", _mock_web_search(True, results))
    monkeypatch.setattr(search_service, "web_fetch", lambda url: _FakeFetch("success"))
    r = search_service.run_web_search(db, "帮我搜索测试关键词", session_id="sess_1")
    assert r["status"] == "success"
    assert r["valid_result_count"] == 3
    assert r["provider"] == "bing"
    run = db.get(SearchRun, r["search_run_id"])
    assert run is not None and run.status == "success"
    assert run.normalized_query == "测试关键词"


def test_search_failed_persists_log(db, monkeypatch):
    """搜索失败：状态落 failed，错误留痕（日志先行，失败也留记录）。"""
    monkeypatch.setattr(search_service, "web_search", _mock_web_search(False, []))
    r = search_service.run_web_search(db, "搜索不存在的服务", session_id="sess_1")
    assert r["status"] == "failed"
    run = db.get(SearchRun, r["search_run_id"])
    assert run is not None and run.status == "failed"
    assert run.error_code == "search_failed"


def test_search_empty_retries_with_alt_query(db, monkeypatch):
    """空结果：自动换关键词重试一次（attempt_count=2），仍无结果落 empty。"""
    calls = []

    def fake(query: str, **kwargs):
        calls.append(query)
        return {"status": "empty", "query": query, "provider": "bing",
                "result_count": 0, "results": [], "error": None}

    monkeypatch.setattr(search_service, "web_search", fake)
    r = search_service.run_web_search(db, "搜索冷门词", session_id="sess_1")
    assert r["status"] == "empty"
    assert len(calls) == 2  # 原词 + 换词重试
    assert calls[1].endswith("介绍")
    run = db.get(SearchRun, r["search_run_id"])
    assert run.attempt_count == 2


def test_search_partial_when_fetch_fails(db, monkeypatch):
    """网页读取部分失败：状态落 partial（搜索成功 ≠ 网页读取成功）。"""
    results = [
        {"title": f"结果{i}", "url": f"https://a.com/{i}", "snippet": "s", "domain": "a.com"}
        for i in range(3)
    ]
    monkeypatch.setattr(search_service, "web_search", _mock_web_search(True, results))

    def fake_fetch(url: str):
        # 第一条读取失败，其余成功 → partial
        return _FakeFetch("failed") if url.endswith("/0") else _FakeFetch("success")

    monkeypatch.setattr(search_service, "web_fetch", fake_fetch)
    r = search_service.run_web_search(db, "搜索关键词", session_id="sess_1")
    assert r["status"] == "partial"


# ---------- web_fetch SSRF 防护 ----------

def test_web_fetch_ssrf_blocked():
    from app.services.web_fetch import web_fetch

    for url in (
        "http://127.0.0.1:8000/docs",
        "http://localhost/api",
        "http://169.254.169.254/latest/meta-data",  # 云元数据
        "http://192.168.1.1/admin",
        "file:///etc/passwd",
        "ftp://example.com/file",
    ):
        r = web_fetch(url)
        assert r.status == "failed", f"{url} 应被 SSRF 防护拒绝"
        assert r.error is not None
