#!/usr/bin/env python3
"""系统默认音色库种子导入：从已部署 TTS workflow meta 初始化 voice_library（2026-11-07）。

数据来源约束（需求 §4.1）：
- speaker_id 必须来自 workflow capabilities.voice_values，禁止虚构或硬编码未验证值
- 描述类元数据（听感年龄/音调/音色特点/性格标签/说话风格）从 voice_descriptions 推导，
  仅收录描述中明确出现的特征；缺失项置 unknown/空，不凭空填写
- 幂等：按 voice_code 稳定映射（speaker_id → voice_code 固定），内容不变不更新

运行方式：在 backend 目录执行 .venv/bin/python scripts/seed_voice_library.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select

from app.core.database import SessionLocal
from app.models import VoiceLibrary, Workflow

# speaker_id → 系统音色元数据映射。
# speaker_id 为部署环境真实值（wf_tts_qwen3_custom_voice.meta.json voice_values，2026-11-07 验证）。
# 特征推导仅取 voice_descriptions 原文明确出现的词，不引申。
VOICE_META_MAP = {
    "Vivian": {
        "voice_code": "female_f", "name": "女性音色 F", "sort_order": 6,
        "perceived_age": "young", "pitch": "unknown",
        "timbre": ["明亮", "略有个性"],
        "personality_tags": [], "speaking_styles": [],
    },
    "Serena": {
        "voice_code": "female_g", "name": "女性音色 G", "sort_order": 7,
        "perceived_age": "young", "pitch": "unknown",
        "timbre": ["温暖", "温柔"],
        "personality_tags": ["温柔"], "speaking_styles": [],
    },
    "Ono_Anna": {
        "voice_code": "female_h", "name": "女性音色 H", "sort_order": 8,
        "perceived_age": "unknown", "pitch": "unknown",
        "timbre": ["轻快", "俏皮"],
        "personality_tags": ["俏皮"], "speaking_styles": ["轻快"],
    },
    "Sohee": {
        "voice_code": "female_i", "name": "女性音色 I", "sort_order": 9,
        "perceived_age": "unknown", "pitch": "unknown",
        "timbre": ["温暖", "富有情感"],
        "personality_tags": [], "speaking_styles": [],
    },
    "Uncle_Fu": {
        "voice_code": "male_a", "name": "男性音色 A", "sort_order": 1,
        "perceived_age": "mature", "pitch": "low",
        "timbre": ["低沉", "醇厚"],
        "personality_tags": ["成熟"], "speaking_styles": [],
    },
    "Dylan": {
        "voice_code": "male_b", "name": "男性音色 B", "sort_order": 2,
        "perceived_age": "young", "pitch": "unknown",
        "timbre": ["清亮", "自然"],
        "personality_tags": [], "speaking_styles": ["自然"],
    },
    "Eric": {
        "voice_code": "male_c", "name": "男性音色 C", "sort_order": 3,
        "perceived_age": "unknown", "pitch": "unknown",
        "timbre": ["略带沙哑", "明亮"],
        "personality_tags": ["活泼"], "speaking_styles": ["活泼"],
    },
    "Ryan": {
        "voice_code": "male_d", "name": "男性音色 D", "sort_order": 4,
        "perceived_age": "unknown", "pitch": "unknown",
        "timbre": ["富有节奏感", "动态"],
        "personality_tags": [], "speaking_styles": [],
    },
    "Aiden": {
        "voice_code": "male_e", "name": "男性音色 E", "sort_order": 5,
        "perceived_age": "unknown", "pitch": "unknown",
        "timbre": ["阳光", "中频清晰"],
        "personality_tags": [], "speaking_styles": [],
    },
}

# 描述中提取的语言（voice_descriptions 明确标注）
LANG_MAP = {
    "Vivian": "中文", "Serena": "中文", "Uncle_Fu": "中文",
    "Dylan": "中文（京腔）", "Eric": "中文（成都话）",
    "Ryan": "英文", "Aiden": "英文（美式）",
    "Ono_Anna": "日语", "Sohee": "韩语",
}


def load_preset_tts_caps(db) -> dict:
    """读取 voice_mode=preset 的 TTS workflow capabilities（与 /speakers 口径一致）。"""
    wfs = db.execute(
        select(Workflow).where(Workflow.workflow_type == "tts").order_by(Workflow.created_at)
    ).scalars().all()
    for w in wfs:
        caps = (w.capabilities or {}) if isinstance(w.capabilities, dict) else {}
        if caps.get("voice_mode") == "preset" and caps.get("voice_values"):
            return {"workflow_id": w.id, "workflow_name": w.name, "caps": caps}
    return {}


def canonical(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def seed() -> None:
    db = SessionLocal()
    created = updated = unchanged = skipped = 0
    try:
        found = load_preset_tts_caps(db)
        if not found:
            print("[severe] 未找到 voice_mode=preset 的 TTS workflow，请先执行 seed_workflows.py")
            sys.exit(1)
        caps = found["caps"]
        display_names = caps.get("voice_display_names") or {}
        descriptions = caps.get("voice_descriptions") or {}
        genders = caps.get("voice_genders") or {}
        speakers = caps.get("voice_values") or []
        print(f"[event] 源 workflow：{found['workflow_name']}，voice_values {len(speakers)} 个")

        existing = {
            v.voice_code: v
            for v in db.execute(select(VoiceLibrary)).scalars().all()
        }

        for speaker_id in speakers:
            meta = VOICE_META_MAP.get(speaker_id)
            if meta is None:
                # workflow 出现未映射音色：跳过并提示，不虚构元数据
                print(f"[warn] speaker {speaker_id} 无映射元数据，跳过（需人工补充映射后重跑）")
                skipped += 1
                continue
            gender = genders.get(speaker_id) or ""
            if gender not in ("male", "female"):
                print(f"[warn] speaker {speaker_id} 性别标注异常（{gender!r}），跳过")
                skipped += 1
                continue
            row = existing.get(meta["voice_code"])
            payload = {
                "voice_code": meta["voice_code"],
                "name": meta["name"],
                "speaker_id": speaker_id,
                "display_name": display_names.get(speaker_id) or speaker_id,
                "gender_presentation": gender,
                "perceived_age": meta["perceived_age"],
                "pitch": meta["pitch"],
                "timbre": meta["timbre"],
                "personality_tags": meta["personality_tags"],
                "speaking_styles": meta["speaking_styles"],
                "language": LANG_MAP.get(speaker_id, "未知"),
                "description": descriptions.get(speaker_id) or "",
                "source_workflow": found["workflow_name"],
                "sort_order": meta["sort_order"],
                "metadata_json": {"derived_from": "voice_descriptions", "verified": "2026-11-07"},
            }
            if row is None:
                db.add(VoiceLibrary(**payload))
                created += 1
            elif canonical({k: getattr(row, k) for k in payload}) != canonical(payload):
                for k, val in payload.items():
                    setattr(row, k, val)
                updated += 1
            else:
                unchanged += 1
        db.commit()
        print(f"[done] 新增 {created} / 更新 {updated} / 不变 {unchanged} / 跳过 {skipped}")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
