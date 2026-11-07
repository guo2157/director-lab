"""视频生成工作流服务：『生成XX视频』请求的端到端会话流程。

流程设计（以『小马过河』为例）：
1. 项目存在性检查：按项目名查重，已存在则提示用户可继续对话，不重复创建
2. 项目不存在：自动创建项目，告知用户，并询问剧本来源（用户自输 / agent 搜索网页资料生成）
3. 剧本来源为 web：搜索网页资料（httpx 抓取百科/故事页），整理后生成剧本
4. 剧本生成后进入确认循环：用户认可 → 保存剧本（ScriptVersion 版本号+1）；
   不认可 → 用户提出改进建议，按建议修改剧本，再次进入确认循环

状态存储：AgentSession.workflow_state（JSON 列），阶段流转：
- idle：无工作流
- ask_script_source：项目已创建，等待用户选择剧本来源
- wait_user_script：等待用户输入剧本
- script_draft：剧本草稿已生成，等待用户确认或改进建议
- script_confirmed：剧本已保存，工作流结束

设计约束（与既有架构一致）：
- 模型只做生成与整理，流程流转由确定性代码执行
- LLM/网络错误按错误码上抛，不落半成品状态
- 剧本保存复用 tool_save_script（单版本覆盖，版本号递增）
"""
import re
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AgentSession, Project

# 显式导入 script_writing：script_create_system 等提示词由该模块在 import 时
# 注册到 prompt_store.DEFAULT_PROMPTS，不导入则 get_prompt 找不到提示词
import app.services.script_writing as _script_writing  # noqa: F401


# --- 工作流阶段常量 ---
STAGE_IDLE = "idle"
STAGE_ASK_STYLE = "ask_style"
STAGE_ASK_SOURCE = "ask_script_source"
STAGE_WAIT_USER_SCRIPT = "wait_user_script"
STAGE_SCRIPT_DRAFT = "script_draft"
STAGE_SCRIPT_CONFIRMED = "script_confirmed"
# 剧本被驳回但用户未给出修改要求：等待用户补充修改说明后再进入修改流程，
# 避免重复询问同一个『是否可以』问题
STAGE_WAIT_REVISION = "wait_revision"

# 网页资料搜索超时（秒）：搜索与抓取共用
_WEB_TIMEOUT = 15.0


def get_workflow_state(session: AgentSession) -> dict:
    """读取会话工作流状态（无状态时返回空 dict）。"""
    return dict(session.workflow_state or {})


def set_workflow_state(db: Session, session: AgentSession, state: dict) -> None:
    """写入会话工作流状态并提交。"""
    session.workflow_state = state
    session.updated_at = datetime.now()
    db.commit()


def extract_story_name(text: str) -> str | None:
    """从『生成一个小马过河的视频/剧本』类消息中提取故事名（如『小马过河』）。"""
    m = re.search(
        r"(?:生成|制作|做|拍|写)(?:一部|一个|个)?(.{2,20}?)(?:的视频|视频|的短片|短片|的动画|动画|的影片|影片|的剧本|剧本)",
        text,
    )
    if m:
        name = m.group(1).strip()
        # 去掉『关于』『有关于』等前缀词
        name = re.sub(r"^(关于|有关于|有关)", "", name).strip()
        # 剥离『第一集/第2期/第一季』这类分集编号：它们是集数表达而非故事名。
        # 否则『生成第一集剧本』会把项目命名成『第一集』，后续剧本也围绕错误主题生成。
        name = re.sub(
            r"第\s*[0-9一二三四五六七八九十百]+\s*(集|期|季|话|章|回|节)$", "", name
        ).strip(" ，,、")
        if 2 <= len(name) <= 20:
            return name
    return None


def is_bare_script_request(text: str) -> bool:
    """识别无故事名的『生成剧本』类请求（如『帮我生成剧本』『写个剧本』）。

    这类请求没有故事名，无法直接进入工作流；由调用方友好询问想创作什么故事，
    避免落入流程操作路由报『未注册的流程操作』。
    """
    return bool(re.search(r"(生成|写|创作|编)(一个|一部|个)?剧本", text))


def find_project_by_name(db: Session, name: str) -> Project | None:
    """按项目名查找未删除的已有项目（精确匹配优先）。"""
    stmt = select(Project).where(Project.deleted_at.is_(None), Project.name == name)
    p = db.scalars(stmt).first()
    if p:
        return p
    # 次选：『XX项目』命名变体
    stmt = select(Project).where(
        Project.deleted_at.is_(None), Project.name.in_([f"{name}项目", f"{name}"])
    )
    return db.scalars(stmt).first()


def detect_orientation(text: str) -> str | None:
    """识别用户对画幅的选择：9:16（竖屏）/ 16:9（横屏）。"""
    if re.search(r"竖屏|竖版|9:16|9：16|手机|竖着", text):
        return "9:16"
    if re.search(r"横屏|横版|16:9|16：9|电影|横着", text):
        return "16:9"
    return None


def detect_style(text: str) -> str | None:
    """识别用户对剧本风格的描述（自由文本，未提及返回 None）。"""
    # 常见风格关键词映射（命中即取，供剧本生成与项目描述使用）
    style_map = [
        (r"童话|寓言|亲子|儿童", "童话寓言"),
        (r"动画|卡通|二次元", "动画"),
        (r"真人|实拍|写实", "真人实拍"),
        (r"喜剧|搞笑|幽默|轻松", "喜剧"),
        (r"教育|科普|益智", "教育科普"),
        (r"冒险|热血|动作", "冒险"),
        (r"温情|治愈|暖心", "温情治愈"),
        (r"古风|国风|水墨", "古风国风"),
    ]
    for pattern, label in style_map:
        if re.search(pattern, text):
            return label
    # 未命中关键词：取『风格是XX / XX风格』中的自由描述
    m = re.search(r"(?:风格(?:是|为)?|想要|希望)(.{2,12}?)(?:风格|的剧本|$)", text)
    if m:
        return m.group(1).strip("，。的 ")
    return None


def detect_style_and_orientation(text: str) -> tuple[str | None, str | None]:
    """识别风格与画幅选择，返回 (style, orientation)。"""
    return detect_style(text), detect_orientation(text)


def detect_script_source_choice(text: str) -> str | None:
    """识别用户对剧本来源的选择：user（自己输入）/ web（agent 搜索网页生成）。
    支持三种回复形式（与提示语『请回复 1 或 2』对齐）：
    - 纯数字/序号：『1』『2』『选2』『第2个』
    - 选项关键词：『自己输入』『搜索』等
    - 完整句子：『我选择网络搜索』等
    """
    stripped = text.strip()
    # 纯数字/序号优先（提示语明确让用户回复 1 或 2，必须能识别）
    num_match = re.fullmatch(r"[选第]?\s*([12])\s*[个条]?\s*[.。,，!！]?\s*", stripped)
    if num_match:
        return "user" if num_match.group(1) == "1" else "web"
    if re.search(r"自己|我来|我输|我自己|自己写|自己输入|我来写|我来提供", text):
        return "user"
    if re.search(r"搜索|网页|网上|资料|你来|你搜|帮我找|自动生成|你来写|你生成|联网", text):
        return "web"
    return None


def detect_script_confirmation(text: str) -> str | None:
    """识别用户对剧本草稿的确认：ok（可以，保存）/ revise（提出改进建议）。

    判定顺序：否定与修改诉求优先，再做肯定确认。
    『不可以』『不行』这类否定词内含『可以』『行』，若先判肯定会被误判为保存，
    若都没命中则会落到调用方的兜底追问，导致用户的驳回被无视。
    """
    stripped = text.strip()
    # 1. 否定或明确修改诉求（优先于肯定确认）
    if re.search(
        r"不可以|不行|不合适|不通过|不同意|不满意|不好|不对|不准确|有误|错误|"
        r"改一下|修改|调整|再改|重新写|重写|重来|重新生成|建议|太长|太短|"
        r"换个|换一|增加|删掉|去掉|补充",
        text,
    ):
        return "revise"
    # 2. 肯定确认：句首为肯定词（『不可以』等否定已被上一步拦下）
    if re.search(
        r"^(可以|可以了|行|好的|没问题|确认|确认保存|同意|通过|保存|ok|OK|Ok|保存吧|就这样|就这个)",
        stripped,
    ):
        return "ok"
    return None


def search_web_materials(story_name: str) -> tuple[str, list[str]]:
    """搜索故事网页资料，返回（整理后的资料文本, 引用 URL 列表）。

    抓取策略：百科类页面优先（内容结构化、正文密度高）；失败不阻断，
    返回空资料由调用方决定降级（提示用户改为自己提供素材）。
    """
    import httpx

    urls: list[str] = []
    snippets: list[str] = []
    # 候选资料源：中文百科/故事站（按优先级尝试，取前 2 个成功结果）
    candidates = [
        f"https://baike.baidu.com/item/{story_name}",
        f"https://zh.wikipedia.org/wiki/{story_name}",
    ]
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        )
    }
    with httpx.Client(timeout=_WEB_TIMEOUT, headers=headers, follow_redirects=True) as client:
        for url in candidates:
            if len(snippets) >= 2:
                break
            try:
                resp = client.get(url)
                if resp.status_code != 200:
                    continue
                # 粗提取正文：去标签、压缩空白，截取有效片段
                body = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", resp.text)
                body = re.sub(r"<[^>]+>", " ", body)
                body = re.sub(r"\s+", " ", body).strip()
                if len(body) < 200:
                    continue
                snippets.append(body[:4000])
                urls.append(url)
            except httpx.HTTPError:
                continue
    # 降级：百科被反爬拦截（403）或无词条时，改用搜索引擎结果页抓取资料摘要
    # （bing/so.com 结果页含标题与摘要文本，可作剧本生成素材）
    if not snippets:
        search_urls = [
            f"https://www.bing.com/search?q={story_name}",
            f"https://www.so.com/s?q={story_name}",
        ]
        with httpx.Client(timeout=_WEB_TIMEOUT, headers=headers, follow_redirects=True) as client:
            for surl in search_urls:
                if len(snippets) >= 2:
                    break
                try:
                    resp = client.get(surl)
                    if resp.status_code != 200:
                        continue
                    body = re.sub(r"<script[\s\S]*?</script>|<style[\s\S]*?</style>", "", resp.text)
                    body = re.sub(r"<[^>]+>", " ", body)
                    body = re.sub(r"\s+", " ", body).strip()
                    # 跳过搜索页导航噪音：优先按结果区标记截取，其次从故事名出现处截取
                    cut_pos = -1
                    for marker in ("为您推荐", "条相关结果", "个结果"):
                        m_pos = body.find(marker)
                        if 0 < m_pos < len(body) // 2:
                            cut_pos = m_pos + len(marker)
                            break
                    if cut_pos < 0:
                        pos = body.find(story_name, 10)
                        cut_pos = pos if pos > 0 else 0
                    body = body[cut_pos:]
                    if len(body) < 500:
                        continue
                    snippets.append(body[:4000])
                    urls.append(surl)
                except httpx.HTTPError:
                    continue
    if not snippets:
        return "", []
    return "\n\n---\n\n".join(snippets), urls


def generate_script_from_materials(
    story_name: str, materials: str, style: str | None = None, orientation: str | None = None
) -> str:
    """基于网页资料调用 LLM 生成短剧剧本（复用剧本创作提示词体系）。

    style/orientation：用户在风格询问阶段选择的剧本风格与画幅，带入生成约束。
    """
    from app.integrations.llm import LLMClient
    from app.services.prompt_store import get_prompt

    system = get_prompt("script_create_system")
    orient_desc = "9:16 竖屏（手机短视频）" if orientation == "9:16" else (
        "16:9 横屏（横版播放）" if orientation == "16:9" else "9:16 竖屏"
    )
    style_desc = f"剧本风格为{style}，" if style else ""
    user = (
        f"请根据以下关于『{story_name}』的网页资料，创作一部适合短视频呈现的短剧剧本。\n"
        f"要求：{style_desc}面向{orient_desc}呈现，忠于原故事主线与人物，"
        "补全场次、动作与对白，单集体量精简。\n\n网页资料：\n" + materials
    )
    client = LLMClient()
    return client.chat(system, user, json_mode=False).strip()


def revise_script(
    story_name: str, draft: str, advice: str, style: str | None = None, orientation: str | None = None
) -> str:
    """按用户改进建议修改剧本草稿（保留主线，仅按建议调整，维持风格与画幅约束）。"""
    from app.integrations.llm import LLMClient

    system = (
        "你是专业短剧编剧。用户会提供一份剧本草稿和改进建议，"
        "请在保留故事主线与人物的前提下，严格按照建议修改剧本。"
        "只输出修改后的完整剧本正文，不输出修改说明。"
    )
    constraint = ""
    if style:
        constraint += f"剧本风格保持{style}；"
    if orientation:
        constraint += f"画幅保持{orientation}（竖屏 9:16 / 横屏 16:9）；"
    user = (
        f"故事：{story_name}\n\n剧本草稿：\n{draft}\n\n改进建议：\n{advice}\n\n"
        f"{constraint}请按建议修改后输出完整剧本。"
    )
    client = LLMClient()
    return client.chat(system, user, json_mode=False).strip()
