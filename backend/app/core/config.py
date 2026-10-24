"""全局配置：所有 TBD 项与外部依赖地址均在此集中管理，禁止在业务代码中写死。

配置优先级：环境变量 > .env 文件 > 默认值。
对应文档：DEVELOPMENT.md、DECISIONS.md 第 3 节（TBD 回填前以默认值 + 可配置形式存在）。
"""
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
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
    comfyui_base_url: str = "https://6006-cpod-1wurfc7eoilo.pod.compshare.cn"
    # 多实例配置（JSON 数组：[{base_url, weight, enabled}]），为空时回退单实例 comfyui_base_url
    comfyui_instances_json: str = ""
    comfyui_timeout_seconds: float = 30.0
    comfyui_poll_interval_seconds: float = 2.0
    comfyui_job_timeout_seconds: float = 600.0  # TBD 关联：超时分类 comfy_timeout

    # --- 语言模型（剧本评估与解析，OpenAI 兼容接口，独立于 ComfyUI） ---
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""  # 凭据只走环境变量 DLAB_LLM_API_KEY，禁止写入代码与日志
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 120.0
    llm_max_retries: int = 2

    # --- 任务调度（D-18 修订 D-03：并发 = 可用 ComfyUI 实例数，单实例串行排队，多实例负载均衡并行） ---
    render_concurrency: int = 1
    worker_poll_interval_seconds: float = 2.0
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

    # --- 媒体存储 ---
    media_dir: Path = Path("./data/media")
    workflow_dir: Path = Path(__file__).resolve().parents[2] / "data" / "workflows"


@lru_cache
def get_settings() -> Settings:
    return Settings()
