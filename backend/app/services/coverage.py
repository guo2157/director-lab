"""解析覆盖率校验：基于 SourceIndex 对解析结果与原文建立逐项对账。

不与 LLM 交互，仅做确定性计算。
"""
from dataclasses import dataclass, field

from app.services.source_index import SourceIndex, SourceBlock


@dataclass
class CoverageReport:
    """覆盖率报告。"""
    episodes: dict = field(default_factory=lambda: {"source": 0, "parsed": 0, "missing": []})
    scenes: dict = field(default_factory=lambda: {"source": 0, "parsed": 0, "missing": []})
    dialogues: dict = field(default_factory=lambda: {
        "source": 0, "matched": 0, "missing": 0, "modified": 0,
    })
    characters: dict = field(default_factory=lambda: {"source": 0, "matched": 0, "missing": []})
    props: dict = field(default_factory=lambda: {"source": 0, "matched": 0, "missing": []})
    overall: str = "passed"  # passed / partial / failed

    def to_dict(self) -> dict:
        return {
            "episodes": self.episodes,
            "scenes": self.scenes,
            "dialogues": self.dialogues,
            "characters": self.characters,
            "props": self.props,
            "overall": self.overall,
        }


def _count_episodes_from_index(index: SourceIndex) -> int:
    """从 SourceIndex 统计原文集数（按 episode 类型区块计数）。"""
    seen: set[str] = set()
    count = 0
    for b in index.blocks:
        if b.kind == "episode" and b.text not in seen:
            seen.add(b.text)
            count += 1
    return count


def _count_scenes_from_index(index: SourceIndex) -> int:
    """从 SourceIndex 统计原文场景数（scene 类型区块）。"""
    return sum(1 for b in index.blocks if b.kind == "scene")


def _count_dialogues_from_index(index: SourceIndex) -> int:
    """从 SourceIndex 统计原文对白数（dialogue 类型区块）。"""
    return sum(1 for b in index.blocks if b.kind == "dialogue")


def _extract_character_names_from_index(index: SourceIndex) -> set[str]:
    """从原文 dialogue 区块中抽取说话人。"""
    import re
    _DLG = re.compile(r"^\s*([^\s：:]{1,40})\s*[：:]")
    names: set[str] = set()
    for b in index.blocks:
        if b.kind == "dialogue":
            m = _DLG.match(b.text)
            if m:
                names.add(m.group(1).strip())
    return names


def _extract_prop_mentions_from_index(index: SourceIndex) -> set[str]:
    """从原文各类型区块中粗抽道具名称（需结合后续精确匹配）。
    
    这是一个启发式抽取：道具通常是名词且出现在动作描述/场景描述中。
    这里返回空集表示需要上游解析结果提供道具清单来对账。
    """
    # 道具抽取属于高精度的 NLP 任务，此处不作为自动盘点手段，
    # 而是依赖解析结果中的 props 列表来对账。
    return set()


def build_coverage_report(
    index: SourceIndex,
    parse_result: dict,
) -> CoverageReport:
    """基于 SourceIndex 与解析结果生成覆盖率报告。"""
    report = CoverageReport()

    # 剧集对账
    src_episodes = _count_episodes_from_index(index)
    parsed_episodes_data = parse_result.get("episodes") or []
    parsed_ep_count = len(parsed_episodes_data)
    report.episodes = {
        "source": src_episodes,
        "parsed": parsed_ep_count,
        "missing": list(range(parsed_ep_count + 1, src_episodes + 1)) if parsed_ep_count < src_episodes else [],
    }

    # 场景对账
    src_scenes = _count_scenes_from_index(index)
    parsed_scenes_data = parse_result.get("scenes") or []
    parsed_sc_count = len(parsed_scenes_data)
    report.scenes = {
        "source": src_scenes,
        "parsed": parsed_sc_count,
        "missing": [],  # 场景划分可能与原文段落不完全对应
    }

    # 对白对账
    src_dialogues = _count_dialogues_from_index(index)
    # 统计解析结果中的对白总数
    parsed_dlg_count = 0
    for sc in parsed_scenes_data:
        for sh in (sc.get("shots") or []):
            segs = sh.get("dialogue_segments") or []
            if segs:
                parsed_dlg_count += len(segs)
            elif sh.get("dialogue"):
                parsed_dlg_count += 1
    report.dialogues = {
        "source": src_dialogues,
        "matched": min(parsed_dlg_count, src_dialogues),
        "missing": max(0, src_dialogues - parsed_dlg_count),
        "modified": 0,  # 需对白对账模块给出精确值
    }

    # 角色对账
    src_chars = _extract_character_names_from_index(index)
    parsed_chars = [
        (c.get("name") or "").strip()
        for c in (parse_result.get("characters") or [])
        if isinstance(c, dict) and c.get("name")
    ]
    parsed_char_names = set(parsed_chars)
    missing_chars = [n for n in src_chars if n not in parsed_char_names]
    report.characters = {
        "source": len(src_chars),
        "matched": len(src_chars - set(missing_chars)),
        "missing": missing_chars,
    }

    # 道具对账
    src_props = _extract_prop_mentions_from_index(index)
    parsed_props = [
        (p.get("name") or "").strip()
        for p in (parse_result.get("props") or [])
        if isinstance(p, dict) and p.get("name")
    ]
    report.props = {
        "source": len(src_props),
        "matched": len(parsed_props),
        "missing": [],
    }

    # 整体判定
    if report.dialogues.get("missing", 0) > src_dialogues * 0.2:
        report.overall = "failed"
    elif report.characters.get("missing") and len(report.characters["missing"]) > 1:
        report.overall = "partial"
    elif report.dialogues.get("missing", 0) > 0 or report.episodes.get("missing"):
        report.overall = "partial"
    else:
        report.overall = "passed"

    return report
