"""对白对账服务：为原文对白生成稳定 ID，与规范化/解析结果逐条匹配，
输出七分类保真报告。

不依赖 LLM，仅依赖原文文本与规范化/解析输出做确定性校验。
"""
import hashlib
import re
from dataclasses import dataclass, field

# 说话人 + 冒号 + 对白正文（兼容中英文冒号）
_DIALOGUE_LINE_RE = re.compile(
    r"^\s*([\u4e00-\u9fa5A-Za-z][\u4e00-\u9fa5A-Za-z0-9_·]{0,9})\s*[:：]\s*(.+?)\s*$",
    re.M,
)

# 归一化比对用：剔除全部标点与空白
_NORMALIZE_RE = re.compile(
    r"[\s，。？！：；、,。?!:;\"'""''「」『』（）()《》〈〉][【】\-_…·~`|/\\*#@&%\$^+=<>]+"
)


def _normalize(text: str) -> str:
    """去除全部标点与空白，只保留语义词序。"""
    return _NORMALIZE_RE.sub("", text)


def _dialogue_id(source_text: str, speaker: str, body: str, block_index: int) -> str:
    """为一条对白生成稳定 ID（与剧本版本无关）。"""
    evidence = "\0".join(("dlg-v1", speaker, body, str(block_index)))
    return f"dlg_{hashlib.sha256(evidence.encode('utf-8')).hexdigest()[:16]}"


@dataclass
class DialogueItem:
    """单条对白记录。"""
    dialogue_id: str
    speaker: str
    text: str
    source_block_ids: list = field(default_factory=list)
    block_index: int = 0
    norm_text: str = ""

    def __post_init__(self):
        if not self.norm_text:
            self.norm_text = _normalize(self.text)


@dataclass
class DialogueAudit:
    """对白保真审计报告。"""
    total: int = 0
    preserved: int = 0
    modified: int = 0
    missing: int = 0
    duplicated: int = 0
    split: int = 0
    merged: int = 0
    unmatched: int = 0
    blocked: bool = False
    details: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "preserved": self.preserved,
            "modified": self.modified,
            "missing": self.missing,
            "duplicated": self.duplicated,
            "split": self.split,
            "merged": self.merged,
            "unmatched": self.unmatched,
            "blocked": self.blocked,
            "details": self.details,
        }


def extract_dialogues(source_text: str) -> list[DialogueItem]:
    """从原文抽取全部对白，每条生成稳定 ID 并记录原文位置。"""
    items: list[DialogueItem] = []
    seen_norm = set()
    for idx, m in enumerate(_DIALOGUE_LINE_RE.finditer(source_text or "")):
        speaker = m.group(1).strip()
        body = m.group(2).strip()
        if not body:
            continue
        did = _dialogue_id(source_text, speaker, body, idx)
        item = DialogueItem(
            dialogue_id=did,
            speaker=speaker,
            text=body,
            block_index=idx,
        )
        if item.norm_text in seen_norm:
            continue  # 完全相同的对白只保留首次出现
        seen_norm.add(item.norm_text)
        items.append(item)
    return items


def extract_normalized_dialogues(normalized_text: str) -> list[DialogueItem]:
    """从规范化/解析结果中抽取对白（只取正文，不含说话人）。"""
    items: list[DialogueItem] = []
    for idx, m in enumerate(_DIALOGUE_LINE_RE.finditer(normalized_text or "")):
        speaker = m.group(1).strip()
        body = m.group(2).strip()
        if not body:
            continue
        items.append(DialogueItem(
            dialogue_id=f"norm_{idx}",
            speaker=speaker,
            text=body,
            block_index=idx,
        ))
    return items


def audit_dialogues(
    source_dialogues: list[DialogueItem],
    normalized_text: str,
    *,
    similarity_threshold: float = 0.85,
) -> DialogueAudit:
    """对原文与规范化结果中的对白进行逐条对账。

    判定优先级（从高到低）：
    1. preserved: 归一化后完全匹配，说话人也一致。
    2. modified: 说话人一致但正文不完全相同（非标点差异）。
    3. missing: 原文对白在规范化结果中找不到对应。
    4. duplicated: 原文同一条对白在结果中出现多次。
    5. split: 原文一条长对白被拆为多条。
    6. merged: 原文多条对白被合并。
    7. unmatched: 规范化结果中出现原文没有的对白。

    无法通过归一化完全匹配时才回退到相似度辅助判断，用于标记 modified。
    """
    audit = DialogueAudit(total=len(source_dialogues))
    norm_dialogues = extract_normalized_dialogues(normalized_text)

    # 建原文索引：归一化指纹 → DialogueItem
    src_by_norm: dict[str, DialogueItem] = {}
    for d in source_dialogues:
        src_by_norm.setdefault(d.norm_text, d)

    # 建规范化结果索引
    norm_norms = [d.norm_text for d in norm_dialogues]
    norm_set = set(norm_norms)

    # 已消费原文下标
    consumed_src: set[int] = set()
    consumed_norm: set[int] = set()

    for si, s_dlg in enumerate(source_dialogues):
        found = False
        for ni, n_dlg in enumerate(norm_dialogues):
            if ni in consumed_norm:
                continue
            # 判定 1：归一完全匹配
            if s_dlg.norm_text == n_dlg.norm_text:
                audit.preserved += 1
                consumed_src.add(si)
                consumed_norm.add(ni)
                audit.details.append({
                    "dialogue_id": s_dlg.dialogue_id,
                    "status": "preserved",
                    "original": s_dlg.text,
                    "normalized": n_dlg.text,
                })
                found = True
                break

        if not found:
            # 尝试相似度辅助匹配（只用于判断是否 modified，不用于判定 preserved）
            best_ni = -1
            best_sim = 0.0
            for ni, n_dlg in enumerate(norm_dialogues):
                if ni in consumed_norm:
                    continue
                sim = _text_similarity(s_dlg.norm_text, n_dlg.norm_text)
                if sim > best_sim:
                    best_sim = sim
                    best_ni = ni

            if best_ni >= 0 and best_sim >= similarity_threshold and s_dlg.speaker:
                # 判定 2：modified（说话人一致但正文不完全相同）
                n_dlg = norm_dialogues[best_ni]
                if s_dlg.speaker == n_dlg.speaker or best_sim >= 0.9:
                    audit.modified += 1
                    consumed_src.add(si)
                    consumed_norm.add(best_ni)
                    audit.details.append({
                        "dialogue_id": s_dlg.dialogue_id,
                        "status": "modified",
                        "original": s_dlg.text,
                        "normalized": n_dlg.text,
                        "similarity": round(best_sim, 3),
                    })
                else:
                    audit.missing += 1
                    audit.details.append({
                        "dialogue_id": s_dlg.dialogue_id,
                        "status": "missing",
                        "original": s_dlg.text,
                    })
            else:
                audit.missing += 1
                audit.details.append({
                    "dialogue_id": s_dlg.dialogue_id,
                    "status": "missing",
                    "original": s_dlg.text,
                    "best_similarity": round(best_sim, 3) if best_ni >= 0 else None,
                })

    # 统计规范化结果中的 unmatched 对白
    for ni in range(len(norm_dialogues)):
        if ni not in consumed_norm:
            audit.unmatched += 1
            audit.details.append({
                "status": "unmatched",
                "normalized": norm_dialogues[ni].text,
            })

    # blocked 判定：有任何实质性丢失则阻断
    audit.blocked = (audit.missing > 0 or audit.modified > 0)
    return audit


def _text_similarity(a: str, b: str) -> float:
    """两个归一化文本的相似度（基于最长公共子序列比）。"""
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    m, n = len(a), len(b)
    if m == 0 or n == 0:
        return 0.0
    # 动态规划求 LCS 长度
    prev = [0] * (n + 1)
    for i in range(1, m + 1):
        curr = [0] * (n + 1)
        for j in range(1, n + 1):
            if a[i - 1] == b[j - 1]:
                curr[j] = prev[j - 1] + 1
            else:
                curr[j] = max(prev[j], curr[j - 1])
        prev = curr
    lcs = prev[n]
    return (2.0 * lcs) / (m + n)
