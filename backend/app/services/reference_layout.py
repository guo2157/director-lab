"""参考图槽位编号 + 提示词职责块的唯一口径（2026-11-21）。

## 问题

分镜视频提示词模板把图序**写死**成：

    【<Picture 1>：上一镜头尾帧】...
    【<Picture 2>：当前镜头分镜图】...

而实际注入的参考图由「工作流声明 + 该图当下是否存在」决定：

- 上一镜视频还没生成 → `ref_prev_frame` 缺失 → 分镜图从第 2 张前移到第 1 张；
- 角色定妆图（`ref_identity` / `ref_character_2..6`）同样占参考图序号，
  但模板从头到尾没提过它们。

于是同一屏出现三种不一致（用户报障原话「提示词和参考图槽位不一致、
槽位缺失、<Picture n> 不全」）：

| 层 | 现象 |
|---|---|
| 编号 | 页面上分镜图显示 `<Picture 2>`，实际它是模型收到的第 1 张 |
| 语义 | 提示词说 Picture 1 是上一镜尾帧，可尾帧根本不存在 |
| 完整性 | 两个角色定妆图占了第 3/4 张，提示词里没有任何描述 |

## 口径

**图片序号 = 实际有图且启用的槽位，按工作流声明顺序压缩后的 1-based 序号。**
这与执行器 `compact_reference_slots()` 的行为一致（剔除空槽位后连续编号），
所以页面显示的编号 == 模型实际收到的图序。

任何「给用户看/给模型看」的 <Picture N> 都必须走 `compressed_numbers()`，
不要再用工作流静态声明里的 `image_no`（那是未压缩的槽位位置，会断档）。
"""
from __future__ import annotations

import re
from typing import Iterable

_REFIMG_RE = re.compile(r"^ref_images\.ref_image_(\d+)$")
_IMAGE_RE = re.compile(r"^images\.image_(\d+)$")
_PICTURE_RE = re.compile(r"<Picture\s*(\d+)>")
_IMAGE_TAG_RE = re.compile(r"<image(\d+)>")
# 职责标题行（行首即标题，后面可能跟同一行的职责正文）：
# 【<Picture 1>：上一镜头尾帧】仅用于…   【<Picture 2>: 当前镜头分镜图】当前镜头…
_DUTY_HEAD_RE = re.compile(r"^【\s*<Picture\s*\d+>\s*[:：]")
# 「承接上一镜头尾帧 <Picture N>」这类跨镜承接短语
_PREV_PICKUP_RE = re.compile(r"承接上一镜头尾帧\s*<Picture\s*\d+>|上一镜头尾帧\s*<Picture\s*\d+>")
# 已重建过的职责块行：- <Picture 1>（当前镜头分镜图）：…
_DUTY_LINE_RE = re.compile(r"^-\s*<(?:Picture|image)\s*(\d+)>\s*[（(]([^）)]*)[）)]")
# 上一镜尾帧当时缺失、被降级过的承接措辞（尾帧后到时要还原）
_PREV_DEGRADED = "按剧情与分镜图推断"
# H3 三段式提示词的正文容器（`to_h3_clip_prompt()` 产出）。
# 职责块必须插在这一行**之后**：模型只读它之后的画面描述。
_H3_CONTAINER = "integrated_multimodal_description"

# 槽位 → (语义类型, 中文标签)
_SLOT_KIND: dict[str, tuple[str, str]] = {
    "ref_prev_frame": ("prev_frame", "上一镜头尾帧"),
    "ref_image": ("keyframe", "当前镜头分镜图"),
    "ref_prop": ("prop", "道具锚定图"),
}


def slot_kind(slot: str | None) -> tuple[str, str]:
    """槽位语义（类型, 中文标签）。角色槽位覆盖 ref_identity / ref_character_2..6。"""
    slot = slot or ""
    if slot in _SLOT_KIND:
        return _SLOT_KIND[slot]
    if slot == "ref_identity" or slot.startswith("ref_character"):
        return ("character", "角色定妆图")
    return ("other", "参考图")


def tag(n: int, syntax: str | None) -> str:
    """按工作流语法渲染图片引用标记。"""
    return f"<image{n}>" if syntax == "image" else f"<Picture {n}>"


def _declared_pairs(reference_slots: list | None) -> list[tuple[int, str]]:
    """[(工作流静态图序, 槽位)]，按静态图序升序。只取图片类且有 input_key 的槽位。"""
    pairs: list[tuple[int, str]] = []
    for item in reference_slots or []:
        if not isinstance(item, dict):
            continue
        param = item.get("param") or item.get("key")
        if not param:
            continue
        key = str(item.get("input_key") or "")
        m = _REFIMG_RE.match(key)
        if m:
            pairs.append((int(m.group(1)) + 1, str(param)))       # ref_image_N → Picture N+1
            continue
        m = _IMAGE_RE.match(key)
        if m:
            pairs.append((int(m.group(1)), str(param)))           # image_N → imageN
    pairs.sort()
    return pairs


def static_numbers(reference_slots: list | None) -> dict[str, int]:
    """槽位 → 工作流静态图序（未压缩，可能断档）。用于把旧提示词里的编号反查回槽位。"""
    return {slot: n for n, slot in _declared_pairs(reference_slots)}


def compressed_numbers(active_slots: Iterable[str], reference_slots: list | None) -> dict[str, int]:
    """槽位 → 实际注入后的图片序号（1-based 连续）。

    active_slots：真正有图、会注入的槽位集合（调用方负责只传这些）。
    顺序沿用工作流声明顺序，与执行器 compact_reference_slots 的压缩结果一致。
    """
    active = {s for s in active_slots if s}
    ordered = [(n, slot) for n, slot in _declared_pairs(reference_slots) if slot in active]
    return {slot: i for i, (_n, slot) in enumerate(ordered, start=1)}


def final_input_key(input_key: str | None, image_no: int | None) -> str | None:
    """把静态声明的 input_key 归一为「压缩后」的实际键名，与 image_no 严格对应。

    两个语法各自保持：
    - ``ref_images.ref_image_N``（H3，0-based）→ ``ref_image_{image_no-1}``
    - ``images.image_N``（Qwen-Image，1-based）→ ``image_{image_no}``

    为什么必须归一：执行器 `compact_reference_slots()` 会删掉未注入的槽位并把
    剩下的键重排成 0..N-1。页面若继续展示静态键名（如分镜图写死
    ``ref_image_1``，而它实际被压缩到 ``ref_image_0``），用户按页面的槽位号
    去对照提示词里的 ``<Picture N>`` 必然错一位。归一后三者（页面槽位、
    input_key、提示词编号）用同一套数字。
    """
    if image_no is None:
        return input_key
    key = str(input_key or "")
    if _REFIMG_RE.match(key):
        return f"ref_images.ref_image_{int(image_no) - 1}"
    if key.startswith("images.image_"):
        return f"images.image_{int(image_no)}"
    return input_key


def build_duty_block(
    numbers: dict[str, int],
    names: dict[str, str],
    syntax: str | None,
    *,
    prev_missing: bool = False,
) -> list[str]:
    """按实际注入槽位生成『参考图职责』块（每张图一行，编号与模型收到的图序一致）。"""
    if not numbers:
        return []
    lines = ["【参考图职责】图序与生成时实际注入的参考图严格一致："]
    char_names: list[str] = []
    for slot, n in sorted(numbers.items(), key=lambda kv: kv[1]):
        kind, label = slot_kind(slot)
        t = tag(n, syntax)
        if kind == "prev_frame":
            lines.append(
                f"- {t}（上一镜头尾帧）：仅用于时间与空间连续性参考（人物位置、站姿、"
                "朝向、视线、道具状态与场景空间关系）。它不是本镜头需要显示的画面，"
                "禁止直接复现、叠加、双重曝光、叠化或保留为背景层。"
            )
        elif kind == "keyframe":
            lines.append(
                f"- {t}（当前镜头分镜图）：本镜头构图、景别、机位、人物站位与外貌的基准"
                "（Ref2VA 参考图，不保证作为强制首帧）。"
            )
        elif kind == "character":
            name = names.get(slot) or "角色"
            char_names.append(name)
            lines.append(
                f"- {t}（角色 @{name} 的定妆图）：正文中 @{name} 即指这张图。"
                "保持其面部特征、发型、服装、体型与配饰一致，不得替换为其他角色。"
            )
        elif kind == "prop":
            name = names.get(slot) or "道具"
            lines.append(f"- {t}（道具 {name} 锚定图）：保持其形状、颜色与材质一致。")
        else:
            name = names.get(slot) or slot
            lines.append(f"- {t}（参考图 {name}）：保持其外观一致。")
    if len(char_names) >= 2:
        lines.append(
            "角色站位固定：只出现上述引用的角色（"
            + "、".join(char_names)
            + "），不得新增、替换、合并角色，不得左右互换站位。"
        )
    if prev_missing:
        lines.append("本镜没有可用的上一镜尾帧，开场状态按剧情与分镜图推断，不要虚构上一镜画面。")
    return lines


def _duty_label_slots(prompt: str, names: dict[str, str]) -> dict[int, str]:
    """从已重建过的【参考图职责】块里反解「图片编号 → 槽位」。

    幂等的关键：重建后的文本已经是**压缩编号**，若再用工作流静态编号反查，
    第二次重建就会把正确引用当成「不存在的图」删掉。这里优先信任块里写明的语义。
    """
    out: dict[int, str] = {}
    for line in prompt.split("\n"):
        m = _DUTY_LINE_RE.match(line.strip())
        if m is None:
            continue
        n = int(m.group(1))
        label = m.group(2)
        if "上一镜头尾帧" in label:
            out[n] = "ref_prev_frame"
        elif "分镜图" in label:
            out[n] = "ref_image"
        elif "道具" in label:
            out[n] = "ref_prop"
        elif label.startswith("角色"):
            hit = re.search(r"@([^\s）)]+)", label)
            if hit is not None:
                name = hit.group(1)
                slot = next((s for s, v in names.items() if v == name), None)
                if slot is not None:
                    out[n] = slot
    return out


def rebuild_prompt(
    prompt: str,
    numbers: dict[str, int],
    names: dict[str, str],
    reference_slots: list | None,
    syntax: str | None = None,
    *,
    prev_missing: bool | None = None,
) -> str:
    """把提示词里的图序重建成『实际注入槽位』口径。

    做四件事，全部是确定性的文本操作：

    1. 删除模板里写死的职责标题行（`【<Picture N>：上一镜头尾帧】` 这类）——
       它们的编号是静态的，与实际图序无关；随后插入统一的【参考图职责】块。
    2. `承接上一镜头尾帧 <Picture N>` 按尾帧是否存在改写：有则指向尾帧新编号，
       无则降级为「按剧情与分镜图推断」，避免指向一张不存在的图。
    3. 正文其余 `<Picture N>` 按「旧静态编号 → 槽位 → 新压缩编号」重映射；
       引用了不存在槽位的**分句**整句删除（保留同一行其他分句）。
    4. 「两张参考图」这类写死数量的措辞改为「上述参考图」。

    幂等：对已重建过的文本再跑一次结果不变（职责块先剥离再重建）。
    """
    if not prompt:
        return prompt
    if prev_missing is None:
        prev_missing = not any(slot_kind(s)[0] == "prev_frame" for s in (numbers or {}))
    static = static_numbers(reference_slots)
    old_to_slot = {n: slot for slot, n in static.items()}
    # 已重建过的职责块语义优先（否则第二次重建会用静态编号误删正确引用）
    old_to_slot.update(_duty_label_slots(prompt, names))

    def _new_no(slot: str | None) -> int | None:
        if slot is None:
            return None
        return numbers.get(slot)

    lines = prompt.split("\n")
    out: list[str] = []
    duty_inserted = False
    # 旧格式职责行识别（历史真实数据里存在两种写法，必须都吃掉）：
    #   ① `- Picture 1：上一镜头尾帧，…`      ← 无尖括号，逗号分隔
    #   ② `- Picture 2：当前镜头分镜图，是…`
    # 不吃掉它们就会与新建的【参考图职责】块并存，同一张图出现两套互相矛盾的职责描述。
    _legacy_duty_re = re.compile(r"^[-*]?\s*(?:Picture|图片|图)\s*\d+\s*[:：]")
    for line in lines:
        stripped = line.strip()
        # 1. 旧的职责标题行整行丢弃（会统一重建）
        if _DUTY_HEAD_RE.match(stripped):
            continue
        if _legacy_duty_re.match(stripped):
            continue
        # 已存在的【参考图职责】块整段丢弃，避免重复与过期编号
        if stripped.startswith("【参考图职责】"):
            continue
        if stripped.startswith("- <Picture") or stripped.startswith("- <image"):
            continue
        if stripped.startswith("角色站位固定：只出现上述引用的角色"):
            continue
        if stripped.startswith("本镜没有可用的上一镜尾帧"):
            continue

        # 2. 跨镜承接短语按尾帧存在与否改写（避免后续把它当普通引用删句）。
        #
        # 这里插入的编号是**新编号**，而第 3 步的重映射把文本里的编号当**旧编号**解释，
        # 两者混在一起就会互相踩：尾帧后到时新插入的「<Picture 1>」会被当成旧的
        # 分镜图编号再映射一次，变成「<Picture 2>」（实测）。
        # 因此改写期间先用占位符，等第 3 步重映射跑完再还原成最终编号。
        _PREV_TOKEN = "\x00PREVFRAME\x00"
        if numbers:
            _prev_slots = [s for s in numbers if slot_kind(s)[0] == "prev_frame"]
            if _prev_slots:
                line = line.replace(
                    _PREV_DEGRADED, f"承接上一镜头尾帧 {_PREV_TOKEN}"
                )

        def _pickup(m: re.Match) -> str:
            prev_slot = next(
                (s for s in numbers if slot_kind(s)[0] == "prev_frame"), None
            )
            if prev_slot is None:
                return _PREV_DEGRADED
            return f"承接上一镜头尾帧 {_PREV_TOKEN}"

        line = _PREV_PICKUP_RE.sub(_pickup, line)

        # 3. 逐分句重映射，含不可用引用的分句整句删除
        if _PICTURE_RE.search(line) or _IMAGE_TAG_RE.search(line):
            parts = re.split(r"([；;])", line)
            kept: list[str] = []
            for part in parts:
                if part in ("；", ";"):
                    kept.append(part)
                    continue
                ok = True
                for m in list(_PICTURE_RE.finditer(part)) + list(_IMAGE_TAG_RE.finditer(part)):
                    old = int(m.group(1))
                    slot = old_to_slot.get(old)
                    if _new_no(slot) is None:
                        ok = False
                        break
                if not ok:
                    # 丢弃该分句及其前导分隔符
                    if kept and kept[-1] in ("；", ";"):
                        kept.pop()
                    continue
                def _remap(m: re.Match) -> str:
                    new = _new_no(old_to_slot.get(int(m.group(1))))
                    return tag(new, syntax) if new is not None else m.group(0)

                part = _PICTURE_RE.sub(_remap, part)
                part = _IMAGE_TAG_RE.sub(_remap, part)
                kept.append(part)
            line = "".join(kept).rstrip("；;")
            if not line.strip():
                continue

        # 承接短语的占位符在第 3 步重映射之后才落成最终编号（见第 2 步注释）
        if _PREV_TOKEN in line:
            _prev_slots = [s for s in numbers if slot_kind(s)[0] == "prev_frame"]
            if _prev_slots:
                line = line.replace(
                    _PREV_TOKEN, tag(numbers[_prev_slots[0]], syntax)
                )
            else:
                line = line.replace(f"承接上一镜头尾帧 {_PREV_TOKEN}", _PREV_DEGRADED)
                line = line.replace(_PREV_TOKEN, "")

        # 4. 写死数量的措辞
        line = line.replace("两张参考图中的同一人物", "上述参考图中的同一人物")

        out.append(line)
        # 职责块插入位置：必须落在 H3 的正文容器**之内**。
        #
        # H3 工作流只从 `integrated_multimodal_description:` 之后读画面描述，
        # 这一行之前的内容模型看不见。早期版本把块插在「任务：」抬头后，
        # 对以 `integrated_multimodal_description:` 开头的三段式提示词，
        # 抬头根本不存在 → 块被插到最顶部 → 职责描述对模型完全无效（实测真实
        # job_51e101cedb80 就是这种格式）。两个锚点按优先级匹配。
        if not duty_inserted and (
            stripped.startswith(_H3_CONTAINER) or stripped.startswith("任务：")
        ):
            out.append("")
            out.extend(build_duty_block(numbers, names, syntax, prev_missing=prev_missing))
            duty_inserted = True

    if not duty_inserted and numbers:
        out.insert(0, "")
        out[0:0] = build_duty_block(numbers, names, syntax, prev_missing=prev_missing)

    # 压缩多余空行（保留段落结构：最多一个空行）
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    if duty_inserted:
        # 职责块前后空行固定为「块前 1 个、块后 1 个」。
        # 重建时旧块被剥掉、新块重新插入，原文本有没有空行会让空行数漂移，
        # 破坏幂等（实测第一遍 0 个、第二遍 1 个）。这里统一收口。
        ls = text.split("\n")
        start = next(
            (i for i, l in enumerate(ls) if l.startswith("【参考图职责】")), None
        )
        if start is not None:
            end = start
            while end + 1 < len(ls) and (
                ls[end + 1].startswith("- <")
                or ls[end + 1].startswith("角色站位固定")
                or ls[end + 1].startswith("本镜没有可用")
            ):
                end += 1
            head = ls[:start]
            while head and not head[-1].strip():
                head.pop()
            tail = ls[end + 1:]
            while tail and not tail[0].strip():
                tail.pop(0)
            body = ls[start:end + 1]
            rebuilt = head + [""] + body
            if tail:
                rebuilt += [""] + tail
            text = "\n".join(rebuilt)
    return text.strip() + "\n" if text.strip() else ""
