"""Phase 4 keyframe image QC 验收测试（A–I）。

覆盖 apply_keyframe_image_qc 的真实代码路径（只 mock 外部依赖：
prompt_qc.apply_keyframe_qc、_gather_reference_images、_call_vision_qc）：

A. 正常通过（prompt_qc ok + vision 无问题）
B. vision 不可用（vision_qc_unavailable，image_qc_ok 仍为 True）
C. 空 prompt 不崩溃
D. 分镜图文件不可读（keyframe_image_unreadable，image_qc_ok=False）
E. prompt_qc 硬阻断降级为告警（prompt_qc_ok=False，pass_ 仍 True）
F. 图像级 issue 检出并透传 code/entity_type
G. 无参考图（reference_images_missing，image_qc_ok 仍为 True）
H. plan_posture_block 透传到 vision 系统提示词
I. 空 shot_plan / {} 容错

附加回归：LLMClient.chat_messages 调用签名必须与真实实现兼容
（曾因传 temperature/timeout_seconds 导致图像级质检静默失效）。
"""

import json
import struct
import zlib
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from app.core.database import make_engine
from app.main import app
from app.models import Base, Shot


# ── helpers ──────────────────────────────────────────────────────────


def _png_bytes() -> bytes:
    """1×1 有效 PNG 字节。"""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    crc = zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF
    ihdr = struct.pack(">I", 13) + b"IHDR" + ihdr_data + struct.pack(">I", crc)
    raw = zlib.compress(b"\x00\xff\x00\x00")
    crc2 = zlib.crc32(b"IDAT" + raw) & 0xFFFFFFFF
    idat = struct.pack(">I", len(raw)) + b"IDAT" + raw + struct.pack(">I", crc2)
    crc3 = zlib.crc32(b"IEND") & 0xFFFFFFFF
    iend = struct.pack(">I", 0) + b"IEND" + struct.pack(">I", crc3)
    return sig + ihdr + idat + iend


def _refs_available():
    """模拟本镜有可用参考图。"""
    return [
        {"type": "scene", "name": "测试场景", "data_uri": "data:image/png;base64,AA=="},
    ]


def _patch_prompt_qc_ok(monkeypatch):
    """提示词级质检通过（不抛硬阻断）。"""
    monkeypatch.setattr(
        "app.services.prompt_qc.apply_keyframe_qc",
        lambda db, shot, prompt, **kw: None,
    )


def _patch_prompt_qc_blocked(monkeypatch, code="prompt_validation_failed"):
    """提示词级质检抛硬阻断异常。"""

    class _QCBlocked(Exception):
        pass

    exc = _QCBlocked("道具锚定图缺失: 钥匙")
    exc.code = code

    def _raise(db, shot, prompt, **kw):
        raise exc

    monkeypatch.setattr("app.services.prompt_qc.apply_keyframe_qc", _raise)
    return code


def _patch_refs(monkeypatch, refs):
    monkeypatch.setattr(
        "app.services.keyframe_image_qc._gather_reference_images",
        lambda db, shot: refs,
    )


def _patch_vision(monkeypatch, result):
    """vision 返回指定 dict；None 表示不可用。"""
    monkeypatch.setattr(
        "app.services.keyframe_image_qc._call_vision_qc",
        lambda system_prompt, keyframe_uri, ref_images: result,
    )


# ── fixtures ────────────────────────────────────────────────────────


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """API TestClient + 隔离 SQLite DB。"""
    engine = make_engine(f"sqlite:///{tmp_path / 'qc_test.db'}")
    Base.metadata.create_all(engine)

    import app.core.database as db_mod

    monkeypatch.setattr(db_mod, "engine", engine)
    monkeypatch.setattr(
        db_mod,
        "SessionLocal",
        sessionmaker(bind=engine, autoflush=False, expire_on_commit=False),
    )

    return TestClient(app)


@pytest.fixture()
def ctx(client, tmp_path):
    """通过 API 创建 project/scene/shot，并补场景基准图满足资产就绪门禁。"""
    from app.core.database import SessionLocal

    pid = client.post(
        "/api/projects", json={"name": "QC Test", "aspect_ratio": "16:9"},
    ).json()["id"]

    scn = client.post(
        "/api/scenes", json={"project_id": pid, "title": "Test Scene", "episode": 1},
    ).json()

    shot_resp = client.post("/api/shots", json={
        "scene_id": scn["id"], "episode": 1, "shot_kind": "visual", "duration": 5,
    }).json()
    shot_id = shot_resp["id"]

    from app.models import Scene
    from tests.conftest import seed_scene_baseline_asset

    db = SessionLocal()
    try:
        _scene = db.get(Scene, scn["id"])
        seed_scene_baseline_asset(db, _scene, str(tmp_path / "media"))
    finally:
        db.close()

    db = SessionLocal()
    try:
        shot = db.get(Shot, shot_id)
        shot.shot_plan = {
            "version": 1,
            "composition": {"shot_size": "medium", "camera_angle": "eye_level"},
        }
        db.commit()
        db.refresh(shot)
        shot_pk = shot.id
    finally:
        db.close()

    return {"db": SessionLocal(), "shot_id": shot_pk, "tmp_path": tmp_path}


@pytest.fixture()
def png_path(ctx):
    p = ctx["tmp_path"] / "frame.png"
    p.write_bytes(_png_bytes())
    return str(p)


# ══════════════════════════════════════════════════════════════════════
# A. 正常通过
# ══════════════════════════════════════════════════════════════════════


class TestA_NormalPass:
    def test_normal_pass(self, ctx, png_path, monkeypatch):
        db = ctx["db"]
        shot = db.get(Shot, ctx["shot_id"])

        _patch_prompt_qc_ok(monkeypatch)
        _patch_refs(monkeypatch, _refs_available())
        _patch_vision(monkeypatch, {"pass": True, "issues": []})

        from app.services.keyframe_image_qc import apply_keyframe_image_qc

        result = apply_keyframe_image_qc(db, shot, "prompt", png_path)

        assert result.pass_ is True
        assert result.prompt_qc_ok is True
        assert result.image_qc_ok is True
        assert result.issues == []
        assert result.warning_count == 0


# ══════════════════════════════════════════════════════════════════════
# B. vision 不可用
# ══════════════════════════════════════════════════════════════════════


class TestB_VisionUnavailable:
    def test_vision_unavailable_does_not_block(self, ctx, png_path, monkeypatch):
        """vision 返回 None → 记 vision_qc_unavailable 告警，image_qc_ok 仍为 True。"""
        db = ctx["db"]
        shot = db.get(Shot, ctx["shot_id"])

        _patch_prompt_qc_ok(monkeypatch)
        _patch_refs(monkeypatch, _refs_available())
        _patch_vision(monkeypatch, None)

        from app.services.keyframe_image_qc import apply_keyframe_image_qc

        result = apply_keyframe_image_qc(db, shot, "prompt", png_path)

        assert result.pass_ is True
        # 不可用属跳过，不算失败
        assert result.image_qc_ok is True
        assert any(i.code == "vision_qc_unavailable" for i in result.issues)
        assert result.warning_count >= 1


# ══════════════════════════════════════════════════════════════════════
# C. 空 prompt
# ══════════════════════════════════════════════════════════════════════


class TestC_EmptyPrompt:
    def test_empty_prompt_does_not_crash(self, ctx, png_path, monkeypatch):
        db = ctx["db"]
        shot = db.get(Shot, ctx["shot_id"])

        _patch_prompt_qc_ok(monkeypatch)
        _patch_refs(monkeypatch, _refs_available())
        _patch_vision(monkeypatch, {"pass": True, "issues": []})

        from app.services.keyframe_image_qc import apply_keyframe_image_qc

        result = apply_keyframe_image_qc(db, shot, "", png_path)
        assert result.pass_ is True


# ══════════════════════════════════════════════════════════════════════
# D. 分镜图文件不可读
# ══════════════════════════════════════════════════════════════════════


class TestD_UnreadableKeyframe:
    def test_missing_file_marks_image_qc_failed(self, ctx, monkeypatch):
        """文件不存在 → keyframe_image_unreadable，image_qc_ok=False，但不阻断。"""
        db = ctx["db"]
        shot = db.get(Shot, ctx["shot_id"])

        _patch_prompt_qc_ok(monkeypatch)

        from app.services.keyframe_image_qc import apply_keyframe_image_qc

        result = apply_keyframe_image_qc(
            db, shot, "prompt", "/nonexistent/path/to/keyframe.png"
        )

        assert result.pass_ is True
        assert result.image_qc_ok is False
        assert any(i.code == "keyframe_image_unreadable" for i in result.issues)


# ══════════════════════════════════════════════════════════════════════
# E. prompt_qc 硬阻断降级
# ══════════════════════════════════════════════════════════════════════


class TestE_PromptQCDowngrade:
    def test_prompt_qc_blocked_downgraded(self, ctx, png_path, monkeypatch):
        db = ctx["db"]
        shot = db.get(Shot, ctx["shot_id"])

        raised_code = _patch_prompt_qc_blocked(monkeypatch)
        _patch_refs(monkeypatch, _refs_available())
        _patch_vision(monkeypatch, {"pass": True, "issues": []})

        from app.services.keyframe_image_qc import apply_keyframe_image_qc

        result = apply_keyframe_image_qc(db, shot, "prompt", png_path)

        assert result.pass_ is True
        assert result.prompt_qc_ok is False
        codes = {i.code for i in result.issues}
        assert raised_code in codes
        # 降级后 severity 统一为 warn
        assert all(i.severity == "warn" for i in result.issues)

    def test_prompt_qc_exception_without_code(self, ctx, png_path, monkeypatch):
        """异常无 code 属性时回退为 prompt_qc_blocked。"""
        db = ctx["db"]
        shot = db.get(Shot, ctx["shot_id"])

        def _raise(db, shot, prompt, **kw):
            raise RuntimeError("boom")

        monkeypatch.setattr("app.services.prompt_qc.apply_keyframe_qc", _raise)
        _patch_refs(monkeypatch, _refs_available())
        _patch_vision(monkeypatch, {"pass": True, "issues": []})

        from app.services.keyframe_image_qc import apply_keyframe_image_qc

        result = apply_keyframe_image_qc(db, shot, "prompt", png_path)

        assert result.prompt_qc_ok is False
        assert any(i.code == "prompt_qc_blocked" for i in result.issues)


# ══════════════════════════════════════════════════════════════════════
# F. 图像级 issue 检出
# ══════════════════════════════════════════════════════════════════════


class TestF_ImageIssuesDetected:
    def test_image_issues_propagated(self, ctx, png_path, monkeypatch):
        db = ctx["db"]
        shot = db.get(Shot, ctx["shot_id"])

        _patch_prompt_qc_ok(monkeypatch)
        _patch_refs(monkeypatch, _refs_available())
        _patch_vision(monkeypatch, {
            "pass": False,
            "issues": [
                {"code": "character_identity", "message": "角色面部与定妆图不符"},
                {"code": "prop_visibility", "message": "道具钥匙不可见"},
                {"code": "composition", "message": "景别偏差，预期中景实际特写"},
            ],
        })

        from app.services.keyframe_image_qc import apply_keyframe_image_qc

        result = apply_keyframe_image_qc(db, shot, "prompt", png_path)

        assert result.pass_ is True
        assert result.image_qc_ok is False
        codes = {i.code for i in result.issues}
        assert {"character_identity", "prop_visibility", "composition"} <= codes
        # 图像级 issue 的 entity_type 固定为 keyframe
        assert all(
            i.entity_type == "keyframe"
            for i in result.issues if i.code in {"character_identity", "prop_visibility"}
        )
        assert all(i.severity == "warn" for i in result.issues)

    def test_vision_issue_default_code(self, ctx, png_path, monkeypatch):
        """vision 返回的 issue 缺 code 时回退 vision_qc_issue。"""
        db = ctx["db"]
        shot = db.get(Shot, ctx["shot_id"])

        _patch_prompt_qc_ok(monkeypatch)
        _patch_refs(monkeypatch, _refs_available())
        _patch_vision(monkeypatch, {"pass": False, "issues": [{"message": "未分类问题"}]})

        from app.services.keyframe_image_qc import apply_keyframe_image_qc

        result = apply_keyframe_image_qc(db, shot, "prompt", png_path)
        assert any(i.code == "vision_qc_issue" for i in result.issues)

    def test_vision_issue_type_description_fields(self, ctx, png_path, monkeypatch):
        """vision 用 type/description 而非 code/message 时也要能解析出内容。

        真实供应商（实测 custom 网关多模态模型）会按自己的习惯返回
        type/description，只认 code/message 会把告警解析成空消息，
        前端看到一条无名 issue 等于没告警。此处锁死双向兼容。
        """
        db = ctx["db"]
        shot = db.get(Shot, ctx["shot_id"])

        _patch_prompt_qc_ok(monkeypatch)
        _patch_refs(monkeypatch, _refs_available())
        _patch_vision(monkeypatch, {
            "issues": [
                {"type": "blank_image", "description": "图像全白，没有可见内容"},
            ],
        })

        from app.services.keyframe_image_qc import apply_keyframe_image_qc

        result = apply_keyframe_image_qc(db, shot, "prompt", png_path)
        vision_issues = [i for i in result.issues if i.entity_type == "keyframe"]
        assert len(vision_issues) == 1, f"实际 {[i.code for i in result.issues]}"
        assert vision_issues[0].code == "blank_image"
        assert vision_issues[0].message == "图像全白，没有可见内容"


# ══════════════════════════════════════════════════════════════════════
# G. 无参考图
# ══════════════════════════════════════════════════════════════════════


class TestG_NoReferenceImages:
    def test_no_reference_images_skips(self, ctx, png_path, monkeypatch):
        """无参考图 → reference_images_missing 告警，image_qc_ok 仍为 True。"""
        db = ctx["db"]
        shot = db.get(Shot, ctx["shot_id"])

        called = {"vision": False}

        _patch_prompt_qc_ok(monkeypatch)
        _patch_refs(monkeypatch, [])
        monkeypatch.setattr(
            "app.services.keyframe_image_qc._call_vision_qc",
            lambda *a, **kw: called.__setitem__("vision", True),
        )

        from app.services.keyframe_image_qc import apply_keyframe_image_qc

        result = apply_keyframe_image_qc(db, shot, "prompt", png_path)

        assert result.pass_ is True
        assert result.image_qc_ok is True
        assert any(i.code == "reference_images_missing" for i in result.issues)
        # 无参考图时不应调用 vision
        assert called["vision"] is False


# ══════════════════════════════════════════════════════════════════════
# H. plan_posture_block 透传
# ══════════════════════════════════════════════════════════════════════


class TestH_PlanPostureBlock:
    def test_posture_block_reaches_system_prompt(self, ctx, png_path, monkeypatch):
        db = ctx["db"]
        shot = db.get(Shot, ctx["shot_id"])
        posture_block = "Alice 站在画面左侧，面向右侧；视线交汇于中央。"

        captured = {}

        _patch_prompt_qc_ok(monkeypatch)
        _patch_refs(monkeypatch, _refs_available())

        def _capture(system_prompt, keyframe_uri, ref_images):
            captured["system_prompt"] = system_prompt
            captured["uri_prefix"] = (keyframe_uri or "")[:22]
            captured["uri_len"] = len(keyframe_uri or "")
            return {"pass": True, "issues": []}

        monkeypatch.setattr(
            "app.services.keyframe_image_qc._call_vision_qc", _capture,
        )

        from app.services.keyframe_image_qc import apply_keyframe_image_qc

        apply_keyframe_image_qc(
            db, shot, "prompt", png_path, plan_posture_block=posture_block,
        )

        assert posture_block in captured["system_prompt"]
        # 分镜图必须以 data URI 形式传入 vision，且已缩放成 JPEG
        # （原样 PNG base64 后单张 3.2MB，一镜四张图请求体 ~13MB，
        #   网关直接 429/超时 → 质检静默跳过，见 keyframe_image_qc 注释）
        assert captured["uri_prefix"] == "data:image/jpeg;base64"
        assert captured["uri_len"] < 400_000, f"data URI 过大：{captured['uri_len']} 字节"

    def test_empty_posture_block_falls_back(self, ctx, png_path, monkeypatch):
        """未传站位块时提示词使用占位文案，不出现 None。"""
        db = ctx["db"]
        shot = db.get(Shot, ctx["shot_id"])

        captured = {}

        _patch_prompt_qc_ok(monkeypatch)
        _patch_refs(monkeypatch, _refs_available())

        def _capture(system_prompt, keyframe_uri, ref_images):
            captured["system_prompt"] = system_prompt
            return {"pass": True, "issues": []}

        monkeypatch.setattr(
            "app.services.keyframe_image_qc._call_vision_qc", _capture,
        )

        from app.services.keyframe_image_qc import apply_keyframe_image_qc

        apply_keyframe_image_qc(db, shot, "prompt", png_path)
        assert "未指定" in captured["system_prompt"]


# ══════════════════════════════════════════════════════════════════════
# I. 空 shot_plan 容错
# ══════════════════════════════════════════════════════════════════════


class TestI_NullShotPlan:
    def _run(self, ctx, png_path, monkeypatch, plan):
        db = ctx["db"]
        shot = db.get(Shot, ctx["shot_id"])
        shot.shot_plan = plan
        db.commit()
        db.refresh(shot)

        _patch_prompt_qc_ok(monkeypatch)
        _patch_refs(monkeypatch, _refs_available())
        _patch_vision(monkeypatch, {"pass": True, "issues": []})

        from app.services.keyframe_image_qc import apply_keyframe_image_qc

        return apply_keyframe_image_qc(db, shot, "prompt", png_path)

    def test_null_shot_plan(self, ctx, png_path, monkeypatch):
        result = self._run(ctx, png_path, monkeypatch, None)
        assert result.pass_ is True
        assert result.image_qc_ok is True

    def test_empty_dict_shot_plan(self, ctx, png_path, monkeypatch):
        result = self._run(ctx, png_path, monkeypatch, {})
        assert result.pass_ is True
        assert result.image_qc_ok is True

    def test_composition_not_dict(self, ctx, png_path, monkeypatch):
        """composition 为非 dict 时不应崩溃。"""
        result = self._run(ctx, png_path, monkeypatch, {"composition": "invalid"})
        assert result.pass_ is True


# ══════════════════════════════════════════════════════════════════════
# 集成：结果结构 / executor payload / vision 调用签名回归
# ══════════════════════════════════════════════════════════════════════


class TestIntegration:
    def test_result_structure_defaults(self):
        from app.services.keyframe_image_qc import KeyframeQCResult

        r = KeyframeQCResult()
        assert r.pass_ is True
        assert r.prompt_qc_ok is True
        assert r.image_qc_ok is True
        assert r.issues == []
        assert r.warning_count == 0

    def test_result_structure_with_issues(self):
        from app.services.keyframe_image_qc import KeyframeQCIssue, KeyframeQCResult

        i = KeyframeQCIssue(code="test", message="test issue")
        r = KeyframeQCResult(issues=[i], prompt_qc_ok=False, image_qc_ok=True)
        assert r.pass_ is True  # pass_ 恒为 True（只告警不阻断）
        assert r.prompt_qc_ok is False
        assert r.image_qc_ok is True
        assert r.warning_count == 1
        assert i.severity == "warn"

    def test_executor_event_payload_serializable(self, ctx, png_path, monkeypatch):
        """issue 可序列化为 executor 的 job.quality_warning payload。"""
        db = ctx["db"]
        shot = db.get(Shot, ctx["shot_id"])

        _patch_prompt_qc_ok(monkeypatch)
        _patch_refs(monkeypatch, _refs_available())
        _patch_vision(monkeypatch, {
            "pass": False,
            "issues": [{"code": "composition", "message": "景别偏差"}],
        })

        from app.services.keyframe_image_qc import apply_keyframe_image_qc

        result = apply_keyframe_image_qc(db, shot, "prompt", png_path)
        assert result.issues

        payload = {
            "asset_id": "ast_test",
            "qc_type": "keyframe_image_qc",
            "prompt_qc_ok": result.prompt_qc_ok,
            "image_qc_ok": result.image_qc_ok,
            "issues": [
                {"code": i.code, "message": i.message} for i in result.issues
            ],
        }
        serialized = json.dumps(payload)
        assert "keyframe_image_qc" in serialized
        assert "composition" in serialized

    def test_call_vision_qc_signature_matches_llm_client(self, monkeypatch, tmp_path):
        """回归：_call_vision_qc 只能传 chat_messages 支持的参数。

        历史缺陷：传了 temperature / timeout_seconds，LLMClient.chat_messages
        不接受这两个参数，抛 TypeError 被 except 吞掉，导致图像级质检
        永远返回 None（静默失效，只留下 vision_qc_unavailable 告警）。
        """
        captured = {}

        class _FakeLLMClient:
            def __init__(self, base_url=None, api_key=None, model=None, timeout=None):
                captured["init"] = {
                    "base_url": base_url, "model": model, "timeout": timeout,
                }

            # 严格复刻真实签名：仅 messages / json_mode / max_tokens
            def chat_messages(self, messages, *, json_mode=False, max_tokens=None):
                captured["messages"] = messages
                captured["json_mode"] = json_mode
                captured["max_tokens"] = max_tokens
                return json.dumps({"pass": True, "issues": []})

        monkeypatch.setattr("app.integrations.llm.LLMClient", _FakeLLMClient)
        monkeypatch.setattr(
            "app.services.keyframe_image_qc.get_settings",
            lambda: MagicMock(
                llm_provider="custom",
                llm_base_url="https://api.example.com/v1",
                llm_api_key="sk-test",
                llm_model="vision-model",
                # 必须显式声明：MagicMock 的属性默认是 truthy 的 Mock 对象，
                # 不写会让 render_mock 守卫误判为开启并直接返回 None。
                render_mock=False,
            ),
        )

        p = tmp_path / "kf.png"
        p.write_bytes(_png_bytes())
        from app.services.keyframe_image_qc import _call_vision_qc

        data_uri = "data:image/png;base64,AA=="
        out = _call_vision_qc("system prompt", data_uri, _refs_available())

        # 关键断言：调用未因未知参数失败，且返回已解析的 dict
        assert out is not None, "chat_messages 调用抛出异常，vision 质检静默失效"
        assert out.get("pass") is True
        assert captured["json_mode"] is True
        assert captured["max_tokens"] == 4096
        # 多模态 messages：首条为分镜图，其后为参考图
        user_content = captured["messages"][1]["content"]
        assert user_content[0]["type"] == "image_url"
        assert user_content[0]["image_url"]["url"] == data_uri

    def test_call_vision_qc_skips_on_unsupported_provider(self, monkeypatch):
        """provider 不在允许集合内时直接返回 None，不发起调用。"""
        called = {"hit": False}

        class _FakeLLMClient:
            def __init__(self, **kw):
                called["hit"] = True

            def chat_messages(self, *a, **kw):
                called["hit"] = True
                return "{}"

        monkeypatch.setattr("app.integrations.llm.LLMClient", _FakeLLMClient)
        monkeypatch.setattr(
            "app.services.keyframe_image_qc.get_settings",
            lambda: MagicMock(
                llm_provider="azure",
                llm_base_url="https://api.example.com/v1",
                llm_api_key="sk-test",
                llm_model="m",
                render_mock=False,  # 同上：确保本用例校验的是 provider 判定分支
            ),
        )

        from app.services.keyframe_image_qc import _call_vision_qc

        assert _call_vision_qc("sp", "data:image/png;base64,AA==", _refs_available()) is None
        assert called["hit"] is False
