"""制作流程状态机（导演台 Agent 执行流程核心）。

将完整制作流程形式化为有序节点链，每个节点绑定：
- 状态判定（基于 derive_progress 的 12 阶段，唯一权威）
- 允许的操作（工具级门禁：操作不得早于其所属节点）
- 人工审核闸口（资产审核 / 分镜审核 / 视频审核）
- 下一步指引（agent 根据当前节点与状态给出）

流程节点链：
  create_project（新建项目）
  → save_script（保存剧本）
  → analyze_script（分析剧本）
  → generate_assets（资产库生成角色图/场景图/道具图）
  → review_assets（人工审核资产库）
  → generate_keyframes（生成分镜图）
  → review_keyframes（人工审核分镜图）
  → generate_clips（批量生成分镜看板视频）
  → review_clips（人工审核视频）
  → compose（合成导出视频）

设计约束：
- 阶段判定以 progress.derive_progress 为唯一权威，本模块不另行推导
- 门禁只拦截『过早』的操作，细粒度阻断（资产缺失/版本失效等）由各业务闸口给出精确错误码
- 审核类节点是闸口：未通过前不得进入下一节点
"""
from dataclasses import dataclass, field
from sqlalchemy.orm import Session
from app.models import Project
from app.services.progress import derive_progress


# --- 流程节点定义 ---

@dataclass(frozen=True)
class PipelineNode:
    key: str                 # 节点标识
    name: str                # 节点中文名
    description: str         # 节点说明
    stages: tuple            # 对应 derive_progress 的阶段集合（节点激活的判定域）
    review_gate: bool = False  # 是否人工审核闸口
    next_hint: str = ""      # 节点完成后的下一步指引


# 流程节点链（有序；stages 为该节点在 12 阶段中的映射域）
PIPELINE_NODES: list[PipelineNode] = [
    PipelineNode(
        key="create_project",
        name="新建项目",
        description="创建制作项目，设定名称、画幅与创作类型",
        stages=(),  # 项目创建前无阶段；项目存在后 estimate 阶段落在保存剧本节点
        next_hint="项目已创建，请保存剧本",
    ),
    PipelineNode(
        key="save_script",
        name="保存剧本",
        description="保存或更新剧本文本（单版本覆盖，版本号递增）",
        stages=("estimate",),
        next_hint="剧本已保存，请发起剧本分析",
    ),
    PipelineNode(
        key="analyze_script",
        name="分析剧本",
        description="剧本一体化分析：解析场景、镜头与资产定义",
        stages=("select_range", "parse"),
        next_hint="剧本分析完成，请到资产库生成角色图/场景图/道具图",
    ),
    PipelineNode(
        key="generate_assets",
        name="资产库生成",
        description="生成角色定妆图、场景基准图、道具锚定图",
        stages=("characters", "scenes"),
        next_hint="公共资产已生成，请人工审核资产库",
    ),
    PipelineNode(
        key="review_assets",
        name="人工审核资产库",
        description="审核角色/场景/道具图无误后放行分镜图生成",
        stages=("assets_review",),
        review_gate=True,
        next_hint="资产审核已通过，可生成分镜图",
    ),
    PipelineNode(
        key="generate_keyframes",
        name="生成分镜图",
        description="为各镜头生成分镜图（关键帧）",
        stages=("keyframes",),
        next_hint="分镜图已生成，请人工审核分镜图",
    ),
    PipelineNode(
        key="review_keyframes",
        name="人工审核分镜图",
        description="逐镜确认分镜图无误（D-13 静图先行闸口）",
        stages=("keyframes",),
        review_gate=True,
        next_hint="分镜图已确认，可批量生成分镜看板视频",
    ),
    PipelineNode(
        key="generate_clips",
        name="生成分镜看板视频",
        description="批量生成分镜看板视频片段（含音频）",
        stages=("clips", "audio"),
        next_hint="看板视频已生成，请人工审核视频",
    ),
    PipelineNode(
        key="review_clips",
        name="人工审核视频",
        description="审核视频片段与音画同步，超容差镜头须放行或重生成",
        stages=("audio",),
        review_gate=True,
        next_hint="视频审核完成，可合成导出成片",
    ),
    PipelineNode(
        key="compose",
        name="合成导出视频",
        description="合成成片并导出交付文件",
        stages=("compose", "export", "done"),
        next_hint="制作流程已完成",
    ),
]

_NODE_BY_KEY: dict[str, PipelineNode] = {n.key: n for n in PIPELINE_NODES}


# --- 操作 → 节点映射（工具级门禁：操作不得早于其所属节点） ---

ACTION_NODE_MAP: dict[str, str] = {
    "save_script": "save_script",
    # 分析剧本的前置是剧本已保存（保存后阶段仍为 estimate，评估标记在分析时同步完成）
    "analyze_script": "save_script",
    "generate_character": "generate_assets",
    "generate_scene": "generate_assets",
    "generate_prop": "generate_assets",
    "review_assets": "review_assets",
    "generate_keyframe": "generate_keyframes",
    "confirm_keyframe": "review_keyframes",
    "generate_clip": "generate_clips",
    "generate_audio": "generate_clips",
    "compose_video": "compose",
    "export_video": "compose",
}


class PipelineStateError(Exception):
    """流程状态机门禁阻断。"""

    def __init__(self, current_node: str, required_node: str, message: str):
        self.current_node = current_node
        self.required_node = required_node
        self.message = message
        super().__init__(message)


@dataclass
class PipelineStatus:
    """项目在流程状态机中的当前位置。"""

    project_id: str
    stage: str                       # 12 阶段（derive_progress 权威值）
    node_key: str                    # 当前流程节点
    node_name: str
    node_description: str
    review_gate: bool                # 当前节点是否审核闸口
    next_hint: str                   # 下一步指引
    shot_total: int = 0
    shot_done: int = 0
    extra: dict = field(default_factory=dict)


def _stage_to_node(stage: str) -> PipelineNode:
    """12 阶段 → 流程节点（取第一个 stages 覆盖该阶段的节点）。"""
    for node in PIPELINE_NODES:
        if stage in node.stages:
            return node
    return PIPELINE_NODES[0]


def get_pipeline_status(db: Session, project_id: str) -> PipelineStatus:
    """查询项目当前流程节点与状态（阶段判定以 derive_progress 为唯一权威）。"""
    project = db.get(Project, project_id)
    if project is None:
        raise PipelineStateError("", "", "项目不存在")
    shot_total, shot_done, stage = derive_progress(db, project)
    node = _stage_to_node(stage)
    extra: dict = {}
    if stage == "keyframes":
        # 细分：全部镜头已有当前有效分镜图但未全部确认 → 审核节点
        from app.models import Scene, Shot
        from app.services.progress import _shot_has_current_asset
        shots = (
            db.query(Shot)
            .join(Scene, Shot.scene_id == Scene.id)
            .filter(Scene.project_id == project_id)
            .all()
        )
        has_kf = [s for s in shots if _shot_has_current_asset(db, s.id, "keyframe")]
        unconfirmed = [s for s in has_kf if not s.keyframe_confirmed]
        if shots and len(has_kf) == len(shots) and unconfirmed:
            node = _NODE_BY_KEY["review_keyframes"]
            extra["unconfirmed_keyframe_count"] = len(unconfirmed)
        else:
            extra["missing_keyframe_count"] = len(shots) - len(has_kf)
            extra["unconfirmed_keyframe_count"] = len(unconfirmed)
    return PipelineStatus(
        project_id=project_id,
        stage=stage,
        node_key=node.key,
        node_name=node.name,
        node_description=node.description,
        review_gate=node.review_gate,
        next_hint=node.next_hint,
        shot_total=shot_total,
        shot_done=shot_done,
        extra=extra,
    )


def _node_index(key: str) -> int:
    for i, n in enumerate(PIPELINE_NODES):
        if n.key == key:
            return i
    return 0


def assert_action_allowed(db: Session, project_id: str, action: str) -> PipelineStatus:
    """操作门禁：action 不得早于其所属流程节点。

    返回当前 PipelineStatus（供调用方附带状态信息）；
    阶段不足时抛 PipelineStateError，不猜测、不降级、不跳过审核闸口。
    """
    status = get_pipeline_status(db, project_id)
    required_key = ACTION_NODE_MAP.get(action)
    if required_key is None:
        return status  # 未映射的操作不做节点门禁（细粒度校验由业务闸口负责）
    required = _NODE_BY_KEY[required_key]
    if _node_index(status.node_key) < _node_index(required_key):
        raise PipelineStateError(
            current_node=status.node_key,
            required_node=required_key,
            message=(
                f"当前流程节点为『{status.node_name}』，执行『{action}』需先完成"
                f"『{required.name}』之前的流程（含人工审核）；下一步：{status.next_hint}"
            ),
        )
    return status


def pipeline_overview() -> list[dict]:
    """流程节点清单（供工具元数据与文档展示）。"""
    return [
        {
            "key": n.key,
            "name": n.name,
            "description": n.description,
            "review_gate": n.review_gate,
            "stages": list(n.stages),
        }
        for n in PIPELINE_NODES
    ]
