"""语言模型客户端：剧本评估与解析的 OpenAI 兼容接口封装。

纪律（ARCHITECTURE.md §1、§5.5、AGENTS.md §3-4）：
- 语言模型为外部服务，独立于 ComfyUI 工作流，不经过渲染底座
- 所有网络调用在短数据库事务之外执行
- 凭据只从配置读取（环境变量 DLAB_LLM_API_KEY），禁止写入日志与任务错误详情
- trust_env=False：忽略环境代理变量，避免被本机代理劫持（同 comfyui 客户端）
错误码（对齐 §5.5 分类风格）：
- 未配置密钥/地址 → llm_unconfigured（配置类）
- 连接失败 → llm_unreachable（环境类，可重试）
- 超时 → llm_timeout（环境类，可重试）
- 接口报错 → llm_error（环境类，可重试）
- 输出非 JSON / 结构非法 → llm_invalid_output（配置类，提示调整提示词）
"""
import json
from typing import Any

import httpx

from app.core.config import get_settings


class LLMError(Exception):
    """语言模型交互错误，携带失败分类错误码（message 不含凭据）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class LLMClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ):
        settings = get_settings()
        self.base_url = (base_url or settings.llm_base_url).rstrip("/")
        self.api_key = api_key if api_key is not None else settings.llm_api_key
        self.model = model or settings.llm_model
        self.timeout = timeout or settings.llm_timeout_seconds

    def _check_config(self) -> None:
        if not self.api_key:
            raise LLMError("llm_unconfigured", "语言模型未配置 API Key（DLAB_LLM_API_KEY）")
        if not self.base_url:
            raise LLMError("llm_unconfigured", "语言模型未配置 base_url")

    def chat(self, system_prompt: str, user_prompt: str, *, json_mode: bool = True) -> str:
        """单次对话调用，返回文本内容。json_mode 时请求 response_format=json_object。"""
        self._check_config()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                resp = client.post(f"{self.base_url}/chat/completions", json=payload, headers=headers)
        except httpx.TimeoutException as e:
            raise LLMError("llm_timeout", f"语言模型请求超时: {type(e).__name__}") from e
        except httpx.ConnectError as e:
            raise LLMError("llm_unreachable", f"语言模型连接失败: {type(e).__name__}") from e
        except httpx.HTTPError as e:
            raise LLMError("llm_unreachable", f"语言模型网络错误: {type(e).__name__}") from e
        if resp.status_code >= 400:
            # 响应体可能含敏感信息，只截取状态码与有限长度，不记录请求头
            raise LLMError("llm_error", f"语言模型接口 HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        try:
            return data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("llm_error", "语言模型响应结构异常") from e

    def chat_json(self, system_prompt: str, user_prompt: str) -> dict:
        """单次对话调用并解析 JSON 输出。模型返回非合法 JSON 时报 llm_invalid_output。"""
        content = self.chat(system_prompt, user_prompt, json_mode=True)
        text = content.strip()
        # 兼容模型在 JSON 外包裹 markdown 代码块的情况
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()
        try:
            result = json.loads(text)
        except json.JSONDecodeError as e:
            raise LLMError("llm_invalid_output", f"语言模型输出非合法 JSON: {e}") from e
        if not isinstance(result, dict):
            raise LLMError("llm_invalid_output", "语言模型输出 JSON 顶层须为对象")
        return result
