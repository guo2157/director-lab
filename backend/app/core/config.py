"""全局配置：所有 TBD 项与外部依赖地址均在此集中管理，禁止在业务代码中写死。

配置优先级：环境变量 > .env 文件 > 默认值。
对应文档：DEVELOPMENT.md、DECISIONS.md 第 3 节（TBD 回填前以默认值 + 可配置形式存在）。
"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_file：支持从 backend/.env 加载配置（优先级：环境变量 > .env > 默认值）
    # 2026-10-28 修复：此前未配置 env_file，写入 .env 的配置静默失效
    model_config = SettingsConfigDict(env_prefix="DLAB_", env_file=".env", extra="ignore")

    # --- 基础 ---
    app_name: str = "director-lab"
    debug: bool = False

    # --- 存储 ---
    data_dir: Path = Path("./data")
    database_url: str = "sqlite:///./data/director_lab.db"

    # --- Mock 渲染（D-11：开启后跳过 ComfyUI 交互，生成占位产物，用于无 ComfyUI 环境的演示与端到端测试） ---
    render_mock: bool = False
    # --- ComfyUI 集成（远端渲染底座） ---
    comfyui_base_url: str = ""
    # 多实例配置（JSON 数组：[{base_url, weight, enabled}]），为空时回退单实例 comfyui_base_url
    comfyui_instances_json: str = ""
    comfyui_timeout_seconds: float = 30.0
    comfyui_poll_interval_seconds: float = 2.0
    comfyui_job_timeout_seconds: float = 600.0  # TBD 关联：超时分类 comfy_timeout

    # --- 语言模型（剧本评估与解析，OpenAI 兼容接口，独立于 ComfyUI） ---
    llm_provider: str = ""  # deepseek/openai/qwen/ollama/custom
    llm_base_url: str = ""
    llm_api_key: str = ""  # 凭据只走环境变量 DLAB_LLM_API_KEY，禁止写入代码与日志
    llm_model: str = ""
    # 多 LLM 实例配置（JSON 数组：[{id, name, provider, base_url, api_key, model, enabled}]），
    # 为空时回退单实例 llm_* 字段；同一时刻仅一个 enabled，后端实时调用启用中的模型
    llm_instances_json: str = ""
    llm_timeout_seconds: float = 600.0
    # 单次生成的最大 token 数（含推理模型的思考 token）。不显式设置时部分供应商默认
    # 仅 4096，长剧本解析会先耗尽预算再输出 shot，表现为「JSON 合法但镜头为空」。
    llm_max_tokens: int = 32768
    # 推理模型（deepseek-v4.x 等）默认带思考链，思考 token 与正文共享 max_tokens 预算。
    # 剧本解析这类纯结构化 JSON 任务里，思考会把预算吃光，表现为「JSON 在字符串中途
    # 被截断」（Expecting ',' delimiter / 非合法 JSON），重试多少次都一样。
    # 剧本解析必须保留推理能力，默认启用 Thinking；仅显式设为 True 时才会发送
    # thinking.type=disabled。供应商不认该字段时客户端自动降级重试。
    llm_disable_thinking: bool = False
    # 流式接收空闲超时（httpx read timeout 语义）：远端连续该时长不吐数据即抛 llm_timeout，
    # 避免 LLM 流式中途停摆时任务线程永久阻塞，把串行队列堵死
    llm_stream_idle_timeout_seconds: float = 900.0
    llm_max_retries: int = 2

    # --- 任务调度（D-18 修订 D-03：并发 = 可用 ComfyUI 实例数，单实例串行排队，多实例负载均衡并行） ---
    render_concurrency: int = 1
    # 单次 LLM 调用最大解析集数（默认 1：每集独立 LLM 调用，从根本上消除 token 溢出）
    max_episodes_per_batch: int = 1
    # 单集解析扩展输出预算（首次截断后放大至此值）
    llm_parse_max_tokens: int = 65536
    # 超长解析硬上限（受模型/供应商上下文限制约束，不可无限放大）
    llm_parse_hard_max_tokens: int = 131072
    # 单集解析默认并发数
    llm_parse_concurrency: int = 3
    # 单个解析任务最大自动重试次数
    llm_parse_retry_limit: int = 2
    # script-agent 首批自动+后续半自动：true 时首次调用仅解析首批（≤max_episodes_per_batch），
    # result.has_more 提示前端『继续分析』；false 时单次调用解析全部批次范围
    agent_first_batch_auto: bool = True
    worker_poll_interval_seconds: float = 2.0
    # 运行期自愈阈值：LLM 类任务处于 running 且长时间无事件、且本进程无执行线程时回收
    llm_job_stall_seconds: float = 900.0
    job_max_attempts: int = 3  # §5.7 重试上限（自动+手动累计），范围 1-10

    # --- 一致性双阈值（T-04：实测后校准，当前为推荐默认值） ---
    consistency_pass_threshold: float = 0.85
    consistency_review_threshold: float = 0.70

    # --- H3 生成参数约束（T-01/T-02：实测前采用文档推荐范围） ---
    h3_duration_min_seconds: int = 4
    h3_duration_max_seconds: int = 15
    h3_default_resolution: str = "768P"

    # --- Stable Audio（T-06：单次上限经验值约 47 秒） ---
    stable_audio_max_seconds: float = 47.0

    # --- TTS 情感映射（T-05：推荐值，未经真实联调验证，见 DECISIONS.md） ---
    tts_emotion_map_json: str = (
        '{"neutral": "", "happy": "Happy", "sad": "Sad", "angry": "Angry", "surprised": "Surprised"}'
    )

    # --- 字幕烧录（PRD M7：burn 模式由 libass 渲染，需要中文字体） ---
    # 留空则自动挑选系统可用的中文字体（fc-list :lang=zh）；显式配置优先
    subtitle_font: str = ""

    # --- 媒体存储 ---
    media_dir: Path = Path("./data/media")
    workflow_dir: Path = Path(__file__).resolve().parents[2] / "data" / "workflows"


_settings_cache: Settings | None = None
_runtime_settings_mtime: float | None = None


def stored_path(value: str | Path) -> Path:
    """把数据库/配置里的存储路径转成本机可用的 Path。

    Windows 下写入的 file_path 会带反斜杠（data\\media\\xxx.png），
    在 Linux 部署时 Path 不把反斜杠当分隔符，会导致图片/视频 404。
    统一替换为正斜杠（Windows 同样接受正斜杠）。
    """
    return Path(str(value).replace("\\", "/"))


def get_settings() -> Settings:
    """获取全局配置（进程内缓存）。

    P1-3 修复（FINAL_CODE_AUDIT_REPORT.md）：设置页保存的配置写入
    data/runtime_settings.json 后，独立 worker 进程需在运行时感知变更。
    以文件 mtime 失效缓存：文件更新后下一次调用返回新配置，无需重启进程。
    """
    global _settings_cache, _runtime_settings_mtime
    p = _runtime_settings_path()
    mtime = None
    try:
        mtime = p.stat().st_mtime
    except OSError:
        mtime = None
    if _settings_cache is not None and mtime == _runtime_settings_mtime:
        return _settings_cache
    s = Settings()
    load_runtime_settings(s)
    _settings_cache = s
    _runtime_settings_mtime = mtime
    return s


def clear_settings_cache() -> None:
    """强制清空配置缓存（测试与显式刷新场景使用，等价原 lru_cache 的 cache_clear）。"""
    global _settings_cache, _runtime_settings_mtime
    _settings_cache = None
    _runtime_settings_mtime = None


# --- 运行时配置持久化（设置页管理，JSON 文件存储；语言模型与 ComfyUI 配置不再从 .env/环境变量读取） ---
# 持久化字段：设置页保存的配置项，重启后保留
_PERSISTED_FIELDS = [
    "comfyui_base_url", "comfyui_instances_json",
    "comfyui_timeout_seconds", "comfyui_job_timeout_seconds",
    "llm_provider", "llm_base_url", "llm_api_key", "llm_model", "llm_instances_json", "llm_timeout_seconds",
    "llm_max_tokens", "llm_disable_thinking",
    "llm_stream_idle_timeout_seconds",
    "job_max_attempts", "consistency_pass_threshold", "consistency_review_threshold",
]


def _runtime_settings_path() -> Path:
    return Path("./data/runtime_settings.json")


def load_runtime_settings(s: Settings) -> None:
    """启动时从 JSON 文件加载设置页管理的配置（覆盖环境变量/.env 值，实现配置以设置页为准）。"""
    import json
    p = _runtime_settings_path()
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return
    for key in _PERSISTED_FIELDS:
        if key in data and data[key] is not None:
            try:
                setattr(s, key, data[key])
            except Exception:
                continue


def save_runtime_settings(s: Settings) -> None:
    """将设置页管理的配置写入 JSON 文件（原子写，避免写坏旧文件）。"""
    import json
    import os
    import tempfile
    p = _runtime_settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {key: getattr(s, key) for key in _PERSISTED_FIELDS}
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
