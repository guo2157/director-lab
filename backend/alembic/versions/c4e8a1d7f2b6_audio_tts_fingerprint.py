"""TTS 复用依赖指纹（A4）

Revision ID: c4e8a1d7f2b6
Revises: b8d2f5c1e9a3
Create Date: 2026-11-18

背景（docs/plan/workflow-dynamic-audit.md 附录 E.4 G4 / v2 计划 §13.5）：
复用 TTS 音频的判据原本只有 `review_status == "approved"`。台词或音色改动后，
旧片段仍是 approved，会被当作「可复用」注入 H3，产出对不上台词的视频。

本迁移：
- `audio_segment.tts_fingerprint` JSON：生成该音频时固化的依赖指纹
  （text_hash / voice_id / tts_workflow_id / tts_workflow_version / params）。
- 全部可空：历史片段没有指纹，读取端按「不可复用」处理并说明原因（不静默当作可复用）。

幂等：SQLite 非事务 DDL，先探测列再添加。
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4e8a1d7f2b6"
down_revision: str | Sequence[str] | None = "b8d2f5c1e9a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _columns(table: str) -> set[str]:
    conn = op.get_bind()
    return {row[1] for row in conn.execute(sa.text(f"PRAGMA table_info({table})"))}


def upgrade() -> None:
    cols = _columns("audio_segment")
    if "tts_fingerprint" not in cols:
        op.add_column("audio_segment", sa.Column("tts_fingerprint", sa.JSON(), nullable=True))


def downgrade() -> None:
    cols = _columns("audio_segment")
    if "tts_fingerprint" in cols:
        op.drop_column("audio_segment", "tts_fingerprint")
