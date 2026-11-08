"""模型基类：所有实体模型统一继承 Base，供 Alembic 收集元数据。

约定（AGENTS.md §4）：ID 使用业务前缀字符串（prj_/scn_/sht_/chr_/prp_/ast_/job_/wf_），
在应用层生成，不使用数据库自增。
"""
from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now(), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=func.now(), server_default=func.now(), onupdate=func.now(), nullable=False
    )
