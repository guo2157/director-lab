"""联网搜索日志模型：搜索执行记录与结果明细（联网搜索整改 Phase 2）。
设计约束（联网搜索整改方案 §5）：
- 每次搜索生成唯一 search_run_id，先落库再执行（日志先行，失败也留痕）
- SearchRun 记录执行状态机流转与错误信息；SearchResult 记录结果条目明细
- 独立于 job 域（搜索是对话域工具，不与生产链路状态机耦合）
- 状态机：pending → searching → validating → fetching → summarizing →
  success / partial / empty / failed / cancelled（终态不可变更）
"""
from datetime import datetime
from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, TimestampMixin


class SearchRun(Base, TimestampMixin):
    """搜索执行记录：一次 web_search 调用的完整生命周期。"""

    __tablename__ = "search_run"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    # 关联会话与项目（可空：通用问答无项目上下文）
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    project_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 用户原始问题与归一化搜索关键词
    user_query: Mapped[str] = mapped_column(Text, nullable=False, default="")
    normalized_query: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 搜索引擎（provider 名，如 bing/so）
    search_provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 状态机：pending/searching/validating/fetching/summarizing/
    # success/partial/empty/failed/cancelled
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)
    result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_result_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class SearchResult(Base, TimestampMixin):
    """搜索结果明细：单条搜索结果及其网页读取状态。"""

    __tablename__ = "search_result"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    search_run_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("search_run.id"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False, default="")
    url: Mapped[str] = mapped_column(Text, nullable=False, default="")
    domain: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    snippet: Mapped[str] = mapped_column(Text, nullable=False, default="")
    published_at: Mapped[str | None] = mapped_column(String(32), nullable=True)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # 网页读取状态：pending / success / failed（未读取为 pending）
    fetch_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    # 内容哈希（正文提取后 sha256 前 16 位，用于去重与变更检测）
    content_hash: Mapped[str | None] = mapped_column(String(16), nullable=True)
