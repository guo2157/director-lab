"""新增接口与描述校验的 HTTP 层单元测试：
- GET /api/workflows/candidates?slot=（asset_image / storyboard_image / video / 未知槽位）
- GET /api/workflows/classification（八组分组 + 无 uncategorized）
- PATCH /api/workflows/{id} 描述长度与唯一性校验（normalize_description）

数据全部来自真实工作流 JSON（backend/data/workflows/），沿用 resolve 测试的
fixture 模式。
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.main import app
from app.models import Base, ids

WF_DIR = Path(__file__).resolve().parents[1] / "data" / "workflows"
# 已退役的工作流定义（数据库记录已删除、seed 不再导入）仍保留 JSON 作为
# 解析/分类逻辑的测试样本，放在 _retired/ 下；读取时回退到这里。
WF_RETIRED_DIR = WF_DIR / "_retired"


def wf_path(name: str) -> Path:
    """工作流 JSON 路径：优先在用目录，其次已退役目录。"""
    p = WF_DIR / f"{name}.json"
    return p if p.exists() else WF_RETIRED_DIR / f"{name}.json"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(engine)
    import app.core.database as db_mod
    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod,
        "SessionLocal",
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )
    with TestClient(app) as c:
        yield c


def _upload(client: TestClient, name: str, wtype: str) -> str:
    resp = client.post(
        "/api/workflows",
        data={"name": name, "workflow_type": wtype},
        files={
            "file": (
                f"{name}.json",
                wf_path(name).read_bytes(),
                "application/json",
            )
        },
    )
    assert resp.status_code == 201, f"上传 {name} 失败: {resp.text}"
    return resp.json()["id"]


def _seed_all(client: TestClient) -> dict[str, str]:
    """导入全部 10 个内置工作流，返回 name → id。"""
    out: dict[str, str] = {}
    spec = [
        ("wf_bgm_stable_audio", "bgm"),
        ("wf_image_ref_qwen21_multi", "image"),
        ("wf_image_t2i_qwen21_turbo", "image"),
        ("wf_tts_qwen3_custom_voice", "tts"),
        ("wf_tts_qwen3_voice_clone", "tts"),
        ("wf_video_h3_flf2v", "video"),
        ("wf_video_h3_ref2va_fast_5s", "video"),
        ("wf_video_h3_ref2va_quality_10s", "video"),
        ("wf_video_h3_ref2va_std_5s", "video"),
        ("wf_video_h3_t2v", "video"),
    ]
    for name, wtype in spec:
        out[name] = _upload(client, name, wtype)
    return out


# --- /api/workflows/candidates 契约 ---------------------------------------


def test_candidates_asset_image_only_pure_t2i_usable(client):
    """资产库槽位：仅纯文生图（无参考图输入）可用，图生图被列为不可用并说明原因。"""
    _seed_all(client)
    resp = client.get("/api/workflows/candidates", params={"slot": "asset_image"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["slot"] == "asset_image"
    assert body["workflow_type"] == "image"
    items = {it["name"]: it for it in body["items"]}
    assert "wf_image_t2i_qwen21_turbo" in items
    assert "wf_image_ref_qwen21_multi" in items
    # 纯文生图可用
    assert items["wf_image_t2i_qwen21_turbo"]["usable"] is True
    assert items["wf_image_t2i_qwen21_turbo"]["missing"] == []
    # 图生图不可用 —— 原因是它声明了图片输入
    assert items["wf_image_ref_qwen21_multi"]["usable"] is False
    assert any("图片输入" in m for m in items["wf_image_ref_qwen21_multi"]["missing"])


def test_candidates_storyboard_image_only_multi_ref_usable(client):
    """分镜图槽位：必须能接多张参考图，纯文生图不可用。"""
    _seed_all(client)
    resp = client.get("/api/workflows/candidates", params={"slot": "storyboard_image"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = {it["name"]: it for it in body["items"]}
    # 多参考图生图可用
    assert items["wf_image_ref_qwen21_multi"]["usable"] is True
    # 纯文生图不可用
    assert items["wf_image_t2i_qwen21_turbo"]["usable"] is False
    assert any(
        "image_input" in m or "multi_reference" in m
        for m in items["wf_image_t2i_qwen21_turbo"]["missing"]
    )


def test_candidates_video_only_h3_ref2va_usable(client):
    """分镜看板槽位：仅有 H3 多参考图视频协议的三套可用，首尾帧与文生视频被排除。"""
    _seed_all(client)
    resp = client.get("/api/workflows/candidates", params={"slot": "video"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    items = {it["name"]: it for it in body["items"]}
    # 三套 H3 ref2va 可用
    for name in (
        "wf_video_h3_ref2va_fast_5s",
        "wf_video_h3_ref2va_std_5s",
        "wf_video_h3_ref2va_quality_10s",
    ):
        assert name in items
        assert items[name]["usable"] is True, f"{name} 应为可用，missing={items[name]['missing']}"
    # 首尾帧与文生视频被排除
    for name in ("wf_video_h3_flf2v", "wf_video_h3_t2v"):
        assert items[name]["usable"] is False, f"{name} 应为不可用"
    # 不可用原因必须包含 multi_reference_to_video
    for name in ("wf_video_h3_flf2v", "wf_video_h3_t2v"):
        assert any(
            "multi_reference_to_video" in m for m in items[name]["missing"]
        ), f"{name} 缺失原因应提及 multi_reference_to_video，实际: {items[name]['missing']}"


def test_candidates_unknown_slot_rejected(client):
    _seed_all(client)
    resp = client.get("/api/workflows/candidates", params={"slot": "hologram"})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_request"


# --- /api/workflows/classification 契约 -----------------------------------


def test_classification_all_10_in_expected_groups_no_uncategorized(client):
    """全部 10 个工作流分进预期分组，无未归类项。"""
    _seed_all(client)
    resp = client.get("/api/workflows/classification")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 10
    assert body["uncategorized"] == []

    by_key: dict[str, list[str]] = {}
    for g in body["groups"]:
        by_key[g["key"]] = [it["name"] for it in g["items"]]

    assert by_key["image_text_to_image"] == ["wf_image_t2i_qwen21_turbo"]
    assert by_key["image_image_to_image"] == ["wf_image_ref_qwen21_multi"]
    assert by_key["video_first_last_frame"] == ["wf_video_h3_flf2v"]
    assert by_key["video_text_to_video"] == ["wf_video_h3_t2v"]
    assert set(by_key["video_reference_to_video"]) == {
        "wf_video_h3_ref2va_fast_5s",
        "wf_video_h3_ref2va_std_5s",
        "wf_video_h3_ref2va_quality_10s",
    }
    assert sorted(by_key["tts"]) == [
        "wf_tts_qwen3_custom_voice",
        "wf_tts_qwen3_voice_clone",
    ]
    assert by_key["bgm"] == ["wf_bgm_stable_audio"]
    assert by_key["sfx"] == []


def test_classification_items_include_generation_modes_and_capability_tags(client):
    """分类返回的每个条目必须带生成模式与能力标签，供前端渲染选择器。"""
    _seed_all(client)
    resp = client.get("/api/workflows/classification")
    body = resp.json()
    for g in body["groups"]:
        for it in g["items"]:
            assert "generation_modes" in it
            assert "description" in it
            assert "workflow_type" in it
            assert "supports_multi_reference" in it


# --- 描述校验（normalize_description 通过 PATCH 验证） ---------------------


def test_patch_description_too_long_chinese_rejected(client):
    """描述超过 20 个汉字应返回 422。"""
    wf_id = _upload(client, "wf_image_t2i_qwen21_turbo", "image")
    # 30 个汉字
    resp = client.patch(
        f"/api/workflows/{wf_id}",
        json={"description": "一二三四五六七八九十一二三四五六七八九十一二三四五六七八九十"},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_request"
    assert "汉字" in detail["message"] or "20" in detail["message"]


def test_patch_description_too_long_english_rejected(client):
    """描述超过 40 个英文字符应返回 422。"""
    wf_id = _upload(client, "wf_image_t2i_qwen21_turbo", "image")
    resp = client.patch(
        f"/api/workflows/{wf_id}",
        json={"description": "a" * 45},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "invalid_request"
    assert "40" in detail["message"]


def test_patch_description_empty_falls_back_to_name(client):
    """PATCH 传入空字符串时，自动回落为工作流名称，不报错。"""
    wf_id = _upload(client, "wf_image_t2i_qwen21_turbo", "image")
    resp = client.patch(f"/api/workflows/{wf_id}", json={"description": ""})
    # 空字符串 → 回落为 wf_image_t2i_qwen21_turbo，200 OK
    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] == "wf_image_t2i_qwen21_turbo"


def test_patch_description_blank_falls_back_to_name(client):
    """PATCH 传入全空格时，自动回落为工作流名称。"""
    wf_id = _upload(client, "wf_video_h3_ref2va_std_5s", "video")
    resp = client.patch(f"/api/workflows/{wf_id}", json={"description": "   "})
    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] == "wf_video_h3_ref2va_std_5s"


def test_patch_description_duplicate_rejected(client):
    """两个工作流不能使用相同的 description。"""
    id_a = _upload(client, "wf_image_t2i_qwen21_turbo", "image")
    id_b = _upload(client, "wf_image_ref_qwen21_multi", "image")
    # 先把 A 的描述改成「我的图」
    resp_a = client.patch(f"/api/workflows/{id_a}", json={"description": "我的图"})
    assert resp_a.status_code == 200
    # B 改成一样的「我的图」→ 422
    resp_b = client.patch(f"/api/workflows/{id_b}", json={"description": "我的图"})
    assert resp_b.status_code == 422
    detail = resp_b.json()["detail"]
    assert detail["code"] == "invalid_request"
    assert "已被工作流" in detail["message"]
    # 唯一性不阻止自己改成自己
    resp_self = client.patch(f"/api/workflows/{id_a}", json={"description": "我的图"})
    assert resp_self.status_code == 200


def test_patch_description_valid_chinese_accepted(client):
    """合法中文描述应保存成功。"""
    wf_id = _upload(client, "wf_video_h3_ref2va_std_5s", "video")
    resp = client.patch(f"/api/workflows/{wf_id}", json={"description": "标准5秒"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] == "标准5秒"
    # 确认 description_source 变为 user
    assert resp.json()["description_source"] == "user"


def test_upload_without_description_generates_from_name(client):
    """上传未填描述时自动使用名称作为描述，description_source 为 seed。"""
    resp = client.post(
        "/api/workflows",
        data={"name": "wf_video_h3_ref2va_std_5s", "workflow_type": "video"},
        files={
            "file": (
                "wf_video_h3_ref2va_std_5s.json",
                wf_path("wf_video_h3_ref2va_std_5s").read_bytes(),
                "application/json",
            )
        },
    )
    assert resp.status_code == 201, resp.text
    wf = resp.json()
    assert wf["description"] == "wf_video_h3_ref2va_std_5s"
    assert wf["description_source"] == "seed"


# --- 各业务槽位的默认推荐工作流 -------------------------------------------


def _recommended(resp) -> dict:
    body = resp.json()
    picked = [it for it in body["items"] if it.get("is_recommended")]
    assert len(picked) == 1, f"槽位 {body['slot']} 应恰好有一个推荐项，实际 {len(picked)}"
    assert body["default_workflow_name"] == picked[0]["name"]
    # 推荐项必须真实可用，不能把默认值指向能力不匹配的模型
    assert picked[0]["usable"] is True
    return picked[0]


def test_candidates_default_asset_image_is_t2i_turbo(client):
    """资产库默认推荐纯文生图 wf_image_t2i_qwen21_turbo。"""
    _seed_all(client)
    resp = client.get("/api/workflows/candidates", params={"slot": "asset_image"})
    assert resp.status_code == 200, resp.text
    assert _recommended(resp)["name"] == "wf_image_t2i_qwen21_turbo"


def test_candidates_default_storyboard_image_is_ref_multi(client):
    """分镜图默认推荐多参考图生图 wf_image_ref_qwen21_multi。"""
    _seed_all(client)
    resp = client.get("/api/workflows/candidates", params={"slot": "storyboard_image"})
    assert resp.status_code == 200, resp.text
    assert _recommended(resp)["name"] == "wf_image_ref_qwen21_multi"


def test_candidates_default_video_is_h3_ref2va_std_5s(client):
    """分镜看板默认推荐多参考图生视频 wf_video_h3_ref2va_std_5s。

    原默认项 wf_video_h3_ref2va_fast_5s 已按用户要求从库中移除，默认改指标准 5 秒版。
    """
    _seed_all(client)
    resp = client.get("/api/workflows/candidates", params={"slot": "video"})
    assert resp.status_code == 200, resp.text
    assert _recommended(resp)["name"] == "wf_video_h3_ref2va_std_5s"
