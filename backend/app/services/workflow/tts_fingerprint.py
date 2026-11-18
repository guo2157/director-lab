"""TTS 复用依赖指纹（A4，v2 计划 §13.5）。

问题：当前复用判据只有 `review_status == "approved"`。台词改了但片段还没重新生成时，
旧音频仍是 approved，会被当作「可复用」注入 H3，产出对不上台词的视频。

解法：为每个音频片段算一个**依赖指纹**，只有指纹未变才算可复用。

指纹输入（与 §13.5 列出的四项对齐，并按本项目真实字段落地）：
- `text`            台词原文（对应 text_hash）
- `voice_id`        实际使用的音色（VoiceProfile.voice_id / Character.voice_id / narrator）
- `tts_workflow_id` TTS 工作流 id
- `tts_workflow_version` TTS 工作流版本号（版本升级即失效）
- `tts_params`      影响音频内容的参数（language / emotion / instruct / speed）

指纹**不包含**：review_status、生成时间、资产版本号 —— 这些变化不代表音频内容失效。

指纹落点：`job.input_snapshot["tts_fingerprint"]`（生成 TTS 时写入），
复用判定时用当前依赖重新计算并比对。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

FINGERPRINT_VERSION = "1.0"

# 影响音频内容的 TTS 参数（顺序固定，保证哈希稳定）
_CONTENT_PARAMS = ("tts_lang", "tts_emotion", "tts_instruct", "tts_speed", "tts_voice")


def _norm_text(text: str | None) -> str:
    """台词归一化：去首尾空白 + 折叠内部空白。空白差异不该导致重生成。"""
    return " ".join((text or "").split())


def compute(
    *,
    text: str | None,
    voice_id: str | None,
    tts_workflow_id: str | None,
    tts_workflow_version: int | str | None,
    tts_params: dict | None = None,
) -> dict:
    """计算依赖指纹，返回可直接落库的结构（含分项，便于人工核对为什么失效）。"""
    params = tts_params or {}
    content = {k: params.get(k) for k in _CONTENT_PARAMS if params.get(k) is not None}
    payload = {
        "v": FINGERPRINT_VERSION,
        "text": _norm_text(text),
        "voice_id": voice_id or "",
        "tts_workflow_id": tts_workflow_id or "",
        "tts_workflow_version": str(tts_workflow_version) if tts_workflow_version is not None else "",
        "params": content,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return {
        "fingerprint": digest,
        "version": FINGERPRINT_VERSION,
        "text_hash": hashlib.sha256(_norm_text(text).encode("utf-8")).hexdigest(),
        "voice_id": voice_id or "",
        "tts_workflow_id": tts_workflow_id or "",
        "tts_workflow_version": payload["tts_workflow_version"],
        "params": content,
    }


@dataclass
class ReuseDecision:
    """复用判定结论。"""

    reusable: bool
    reason: str
    changed: list[str] = field(default_factory=list)
    stored: dict | None = None
    current: dict | None = None
    # 历史片段无指纹、按宽限期放行时为 True：调用方必须留痕告警（不能静默复用）
    legacy: bool = False

    def to_dict(self) -> dict:
        return {
            "reusable": self.reusable,
            "reason": self.reason,
            "changed": self.changed,
            "legacy": self.legacy,
            "stored_fingerprint": (self.stored or {}).get("fingerprint"),
            "current_fingerprint": (self.current or {}).get("fingerprint"),
        }


def _changed_fields(stored: dict, current: dict) -> list[str]:
    """逐项比对，返回变化的字段名（用于告诉用户「为什么不能用旧音频」）。"""
    changed: list[str] = []
    if stored.get("text_hash") != current.get("text_hash"):
        changed.append("text")
    if stored.get("voice_id") != current.get("voice_id"):
        changed.append("voice_id")
    if stored.get("tts_workflow_id") != current.get("tts_workflow_id"):
        changed.append("tts_workflow_id")
    if stored.get("tts_workflow_version") != current.get("tts_workflow_version"):
        changed.append("tts_workflow_version")
    if (stored.get("params") or {}) != (current.get("params") or {}):
        changed.append("tts_params")
    if stored.get("version") != current.get("version"):
        changed.append("fingerprint_version")
    return changed


def check_reuse(
    *,
    stored: dict | None,
    text: str | None,
    voice_id: str | None,
    tts_workflow_id: str | None,
    tts_workflow_version: int | str | None,
    tts_params: dict | None = None,
    approved: bool = True,
    audio_exists: bool = True,
    legacy_grace: bool = True,
) -> ReuseDecision:
    """判定已有 TTS 音频能否复用。

    复用需同时满足：
    1. 片段已审核通过（人工确认过内容）
    2. 音频文件仍存在（文件被清理时不能复用）
    3. 依赖指纹未变化（台词/音色/TTS 工作流版本/内容参数都没动）

    缺指纹（A4 上线前的历史数据）有两种处理方式：
    - `legacy_grace=True`（默认，宽限期）：按旧逻辑放行，但返回 `legacy=True`，
      调用方**必须**写 `audio.fingerprint_missing` 事件告警，等片段重新生成后自然带上指纹；
    - `legacy_grace=False`（严格模式）：一律判不可复用。
    两种方式都不静默——宽限期也留痕，只是不阻断存量项目。
    """
    current = compute(
        text=text, voice_id=voice_id,
        tts_workflow_id=tts_workflow_id,
        tts_workflow_version=tts_workflow_version,
        tts_params=tts_params,
    )
    if not approved:
        return ReuseDecision(False, "片段未审核通过，需先审核或重新生成", stored=stored, current=current)
    if not audio_exists:
        return ReuseDecision(False, "音频文件不存在，需重新生成", stored=stored, current=current)
    if not stored or not stored.get("fingerprint"):
        if legacy_grace:
            return ReuseDecision(
                True,
                "缺少依赖指纹（A4 上线前的历史片段），宽限期按审核状态复用；"
                "重新生成该片段后会自动补齐指纹",
                stored=stored, current=current, legacy=True,
            )
        return ReuseDecision(
            False, "缺少依赖指纹（历史片段），无法确认音频与当前台词/音色一致",
            stored=stored, current=current,
        )
    # 算法版本先比对：旧算法算出的指纹即使字符串相同也不能直接采信
    if stored.get("version") != current.get("version"):
        return ReuseDecision(
            False,
            f"指纹算法版本已升级（{stored.get('version')} → {current.get('version')}），需重新生成 TTS",
            changed=["fingerprint_version"], stored=stored, current=current,
        )
    if stored.get("fingerprint") == current["fingerprint"]:
        return ReuseDecision(True, "依赖未变化，复用已有 TTS 音频", stored=stored, current=current)

    changed = _changed_fields(stored, current)
    labels = {
        "text": "台词",
        "voice_id": "音色",
        "tts_workflow_id": "TTS 工作流",
        "tts_workflow_version": "TTS 工作流版本",
        "tts_params": "TTS 参数",
        "fingerprint_version": "指纹算法版本",
    }
    human = "、".join(labels.get(c, c) for c in changed) or "依赖"
    return ReuseDecision(
        False, f"{human}已变更，需重新生成 TTS",
        changed=changed, stored=stored, current=current,
    )
