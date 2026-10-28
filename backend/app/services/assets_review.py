"""公共资产人工审核状态（D-23）：资产库 → 分镜图 的必经闸口。

规则：审核结论只对『审核当时的资产版本』有效。任何会改变公共资产
（角色定妆图 / 场景基准图 / 道具锚定图）的操作，都必须把审核状态
回退为 pending，避免审核通过后改图仍直接放行分镜图。
"""
from sqlalchemy.orm import Session

from app.models import Project


def reset_assets_review(db: Session, project_id: str) -> bool:
    """把项目审核状态回退为待审核（不提交事务，由调用方 commit）。

    返回是否发生回退；已是 pending 时不重复写库。
    """
    if not project_id:
        return False
    project = db.get(Project, project_id)
    if project is None:
        return False
    if (project.assets_review_status or "pending") == "pending":
        return False
    project.assets_review_status = "pending"
    project.assets_reviewed_at = None
    return True
