"""从剧本原文确定性派生行级证据区块，不改写或持久化原文。"""
import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

SOURCE_INDEX_ALGORITHM = "source-line-v1"

_NUMBER = r"(?:\d+|[零〇一二三四五六七八九十百千万两]+)"
_EPISODE_RE = re.compile(rf"^\s*第\s*{_NUMBER}\s*集(?:\s|$|[:：.、])")
_CHAPTER_RE = re.compile(rf"^\s*第\s*{_NUMBER}\s*章(?:\s|$|[:：.、])")
_SCENE_RE = re.compile(
    rf"^\s*(?:第\s*{_NUMBER}\s*场(?:\s|$|[:：.、])|"
    r"场次\s*\d+(?:\s|$|[:：.、])|"
    r"(?:INT\.?/EXT\.?|INT\.?|EXT\.?|I/E\.?)\s|"
    r"(?:\d+\s*[.、-]\s*)?(?:内外|外内|内|外)(?:景)?\s*(?:[/／|]|$))",
    re.IGNORECASE,
)
_NARRATION_RE = re.compile(
    r"^\s*(?:旁白|画外音|场景描述|环境描述|动作描述|VO|V\.O\.|OS|O\.S\.)\s*[：:]",
    re.IGNORECASE,
)
_DIALOGUE_RE = re.compile(r"^\s*[^\s：:]{1,40}\s*[：:]\s*\S")
_LINE_RE = re.compile(r"([^\r\n]*)(\r\n|\r|\n|$)")


@dataclass(frozen=True, slots=True)
class SourceBlock:
    source_block_id: str
    script_version_id: str
    script_version: int
    seq: int
    line: int
    kind: str
    start: int
    end: int
    text: str
    text_sha256: str

    def to_dict(self) -> dict:
        return {
            "source_block_id": self.source_block_id,
            "script_version_id": self.script_version_id,
            "script_version": self.script_version,
            "seq": self.seq,
            "line": self.line,
            "kind": self.kind,
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "text_sha256": self.text_sha256,
        }


@dataclass(frozen=True, slots=True)
class SourceIndex:
    algorithm: str
    script_version_id: str
    script_version: int
    blocks: tuple[SourceBlock, ...]

    def to_dict(self) -> dict:
        return {
            "algorithm": self.algorithm,
            "script_version_id": self.script_version_id,
            "script_version": self.script_version,
            "block_count": len(self.blocks),
            "blocks": [block.to_dict() for block in self.blocks],
        }


def _classify(text: str) -> str:
    if _EPISODE_RE.match(text):
        return "episode"
    if _CHAPTER_RE.match(text):
        return "chapter"
    if _SCENE_RE.match(text):
        return "scene"
    if _NARRATION_RE.match(text):
        return "narration"
    if _DIALOGUE_RE.match(text):
        return "dialogue"
    return "paragraph"


def _block_id(*, script_version_id: str, version: int, start: int, kind: str, text: str) -> str:
    evidence = "\0".join(
        (SOURCE_INDEX_ALGORITHM, script_version_id, str(version), str(start), kind, text)
    )
    return f"sb_{hashlib.sha256(evidence.encode('utf-8')).hexdigest()[:32]}"


def build_source_index(
    content: str, *, script_version_id: str, version: int
) -> SourceIndex:
    """按 CR/LF 物理行建索引；空行跳过，非空行保留全部原始字符。"""
    blocks: list[SourceBlock] = []
    offset = 0
    line = 1
    while offset < len(content):
        match = _LINE_RE.match(content, offset)
        if match is None:  # pragma: no cover - 正则覆盖所有剩余字符
            break
        text = match.group(1)
        end = offset + len(text)
        if text.strip():
            kind = _classify(text)
            blocks.append(SourceBlock(
                source_block_id=_block_id(
                    script_version_id=script_version_id,
                    version=version,
                    start=offset,
                    kind=kind,
                    text=text,
                ),
                script_version_id=script_version_id,
                script_version=version,
                seq=len(blocks) + 1,
                line=line,
                kind=kind,
                start=offset,
                end=end,
                text=text,
                text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            ))
        offset = match.end()
        line += 1
    return SourceIndex(
        algorithm=SOURCE_INDEX_ALGORITHM,
        script_version_id=script_version_id,
        script_version=version,
        blocks=tuple(blocks),
    )


def validate_source_references(
    index: SourceIndex, references: Iterable[Mapping[str, object]]
) -> dict:
    """返回逐项校验问题；任一证据不符时 valid=false，不修改索引或剧本。"""
    block_by_id = {block.source_block_id: block for block in index.blocks}
    issues: list[dict] = []
    checked = 0
    for reference_index, reference in enumerate(references):
        checked += 1
        block_id = reference.get("source_block_id")
        block = block_by_id.get(block_id) if isinstance(block_id, str) else None
        if block is None:
            issues.append({
                "reference_index": reference_index,
                "source_block_id": block_id,
                "code": "source_block_not_found",
                "message": "原文区块不存在或不属于当前剧本版本",
            })
            continue
        checks = (
            ("script_version_id", "script_version_id_mismatch", block.script_version_id),
            ("script_version", "script_version_mismatch", block.script_version),
            ("start", "start_mismatch", block.start),
            ("end", "end_mismatch", block.end),
            ("text", "text_mismatch", block.text),
        )
        for field, code, expected in checks:
            actual = reference.get(field)
            if actual is not None and actual != expected:
                issues.append({
                    "reference_index": reference_index,
                    "source_block_id": block_id,
                    "code": code,
                    "field": field,
                    "expected": expected,
                    "actual": actual,
                })
    return {"valid": not issues, "checked": checked, "issues": issues}
