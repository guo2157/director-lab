"""生产实体：资产、资产引用、工作流、任务、解析版本、依赖关系、事件 outbox、上传记录、合成、导出。

权威定义：DATABASE.md §2 核心表 + §3 索引与约束。
"""
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models import ids


class Asset(Base):
    """资产：版本化产物记录。"""

    __tablename__ = "asset"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.asset_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("project.id"), nullable=False)
    asset_type: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    parent_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    thumb_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ref_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    validity: Mapped[str] = mapped_column(String(16), nullable=False, default="current", server_default="current")
    input_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # 图片质量检查（P3）：pending=待检查 / passed=通过 / failed=失败；issues 记录具体问题
    quality_status: Mapped[str | None] = mapped_column(String(16), nullable=True, default="pending", server_default="pending")
    quality_issues: Mapped[dict | None] = mapped_column(JSON, nullable=True)


Index("ix_asset_project_type_validity", Asset.__table__.c.project_id, Asset.__table__.c.asset_type, Asset.__table__.c.validity)


class AssetRef(Base):
    """镜头-资产绑定：只表达引用关系，不用于判断产物是否具备。"""

    __tablename__ = "asset_ref"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.asset_ref_id)
    shot_id: Mapped[str] = mapped_column(String(32), ForeignKey("shot.id"), nullable=False, index=True)
    asset_id: Mapped[str] = mapped_column(String(32), ForeignKey("asset.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Workflow(TimestampMixin, Base):
    """工作流：可插拔能力单元。"""

    __tablename__ = "workflow"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.workflow_id)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    workflow_type: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    trigger: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    capabilities: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    definition: Mapped[dict] = mapped_column(JSON, nullable=False)
    parsed_schema: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")


class Job(Base):
    """任务：生成/解析/合成/导出执行单元。"""

    __tablename__ = "job"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.job_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("project.id"), nullable=False)
    shot_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("shot.id"), nullable=True)
    job_type: Mapped[str] = mapped_column(String(32), nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(String(32), ForeignKey("workflow.id"), nullable=True)
    workflow_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    params: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="queued", server_default="queued")
    prompt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    error_code: Mapped[str | None] = mapped_column(String(48), nullable=True)
    error_msg: Mapped[str | None] = mapped_column(Text, nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    request_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    submission_state: Mapped[str] = mapped_column(String(24), nullable=False, default="not_submitted", server_default="not_submitted")
    input_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    event_seq: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


Index("ix_job_project_status", Job.__table__.c.project_id, Job.__table__.c.status)
Index("ix_job_prompt_id", Job.__table__.c.prompt_id)
Index("ix_job_status_type", Job.__table__.c.status, Job.__table__.c.job_type)
Index("ix_job_created_at", Job.__table__.c.created_at)
Index("ix_job_status_priority", Job.__table__.c.status, Job.__table__.c.priority)


class ParseVersion(Base):
    """解析版本。"""

    __tablename__ = "parse_version"
    __table_args__ = (UniqueConstraint("project_id", "version", name="uq_parse_version_project_version"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.parse_version_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("project.id"), nullable=False, index=True)
    script_version_id: Mapped[str] = mapped_column(String(32), ForeignKey("script_version.id"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="candidate", server_default="candidate")
    diff_summary: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # script-agent 口径锁定：首批解析成功后锁定总集数；script_hash 变化则口径失效
    total_episodes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    script_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DependencyRef(Base):
    """依赖关系（权威来源）：上游版本变化驱动下游失效传播。"""

    __tablename__ = "dependency_ref"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.dependency_ref_id)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(32), nullable=False)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_type: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[str] = mapped_column(String(32), nullable=False)
    dependency_role: Mapped[str] = mapped_column(String(24), nullable=False)
    validity: Mapped[str] = mapped_column(String(16), nullable=False, default="current", server_default="current")


Index("ix_dependency_ref_target", DependencyRef.__table__.c.target_type, DependencyRef.__table__.c.target_id)


class JobEvent(Base):
    """事件 outbox：任务状态与事件同事务写入，提交前禁止发布。"""

    __tablename__ = "job_event"

    event_seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(32), ForeignKey("job.id"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UploadRecord(Base):
    """上传记录：本地文件到 ComfyUI 文件名的映射。"""

    __tablename__ = "upload_record"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.upload_record_id)
    comfy_filename: Mapped[str] = mapped_column(String(300), nullable=False)
    local_path: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Composition(Base):
    """合成产物：每次 compose 调用创建一条记录。"""

    __tablename__ = "composition"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.composition_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("project.id"), nullable=False)
    episode: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="composing", server_default="composing")
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    srt_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    source_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    warnings: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


Index("ix_composition_project_episode_stale", Composition.__table__.c.project_id, Composition.__table__.c.episode, Composition.__table__.c.stale)


class Export(Base):
    """导出记录：每次 export 调用创建一条记录。"""

    __tablename__ = "export"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=ids.export_id)
    project_id: Mapped[str] = mapped_column(String(32), ForeignKey("project.id"), nullable=False)
    composition_id: Mapped[str] = mapped_column(String(32), ForeignKey("composition.id"), nullable=False)
    format: Mapped[str] = mapped_column(String(8), nullable=False, default="mp4", server_default="mp4")
    delivery_profile: Mapped[str] = mapped_column(String(16), nullable=False, default="standard", server_default="standard")
    file_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="exporting", server_default="exporting")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
