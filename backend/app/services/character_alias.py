"""角色别名与群体分类（P1-八 步骤2）。

只做规则判定与留痕，不改写模型给出的名称与剧情数据：
- split_entity_alias：从『张三（三哥/张三丰）』这类名称里拆出主名与括号别名；
- classify_character_type：按群体词与后缀判定 individual / crowd，
  避免为『众人』『村民们』这类群体生成定妆图与配音；
- build_alias_record：落库前统一入口，返回主名、别名列表与角色类型。
"""
import re

# 群体词：名称指向一群人而非特定个体。判定不出的保持 individual 交人工确认。
_CROWD_KEYWORDS = (
    "众人", "群众", "人群", "百姓", "村民", "乡亲", "路人", "行人", "围观",
    "士兵", "侍卫", "守卫", "家丁", "丫鬟", "仆人", "随从", "手下", "保镖",
    "宾客", "客人", "群臣", "大臣", "官员", "记者", "粉丝", "观众", "敌军",
    "军队", "匪徒", "黑衣人", "宫女", "太监",
)
_CROWD_SUFFIXES = ("们", "众")
_PAIR_BRACKETS = r"[（(]([^（）()]*)[)）]"
_ALIAS_SEPARATORS = r"[/／、,，;；]"


def split_entity_alias(name: str) -> tuple[str, list[str]]:
    """拆分实体名与括号别名：『张三（三哥/张三丰）』→ ('张三', ['三哥', '张三丰'])。

    只处理未嵌套的成对中/英文括号，按常见分隔符切成多个别名。
    括号里可能是定位补充而非别名，这里不做语义判断，由调用方决定是否采信。
    """
    raw = (name or "").strip()
    if not raw:
        return "", []
    aliases: list[str] = []
    for m in re.finditer(_PAIR_BRACKETS, raw):
        for part in re.split(_ALIAS_SEPARATORS, m.group(1)):
            alias = (part or "").strip()
            if alias:
                aliases.append(alias)
    main = re.sub(_PAIR_BRACKETS, "", raw).strip() or raw
    seen = set()
    uniq: list[str] = []
    for a in aliases:
        if a == main or a in seen:
            continue
        seen.add(a)
        uniq.append(a)
    return main, uniq


def classify_character_type(name: str, explicit=None) -> str:
    """角色类型：individual（个体）/ crowd（群体）。

    模型显式给出 individual/crowd 时优先采信；否则按群体词与『们/众』后缀判定。
    """
    if isinstance(explicit, str) and explicit.strip().lower() in ("individual", "crowd"):
        return explicit.strip().lower()
    raw = (name or "").strip()
    if not raw:
        return "individual"
    main, _ = split_entity_alias(raw)
    for token in (main, raw):
        if not token:
            continue
        if token.endswith(_CROWD_SUFFIXES):
            return "crowd"
        for kw in _CROWD_KEYWORDS:
            if kw in token:
                return "crowd"
    return "individual"


def build_alias_record(name: str, explicit_type=None) -> tuple[str, list[str], str]:
    """落库前统一入口：返回 (主名, 别名列表, 角色类型)。"""
    main, aliases = split_entity_alias(name)
    return main, aliases, classify_character_type(name, explicit_type)
