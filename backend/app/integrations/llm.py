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
import time
from typing import Any

import httpx

from app.core.config import get_settings

# 已确认不支持 thinking 字段的端点（base_url）：命中后不再携带该字段，保证兼容性
_THINKING_UNSUPPORTED: set[str] = set()


class LLMError(Exception):
    """语言模型交互错误，携带失败分类错误码（message 不含凭据）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _active_llm_instance(settings) -> dict | None:
    """解析多 LLM 实例配置（llm_instances_json），返回当前启用的实例（同一时刻仅一个 enabled）。

    配置格式：[{id, name, provider, base_url, api_key, model, enabled}]
    未配置或解析失败时返回 None，调用方回退单实例 llm_* 字段。
    api_key 为掩码（以 * 开头）时视为未修改，回退到单实例已保存密钥。
    """
    import json
    raw = getattr(settings, "llm_instances_json", "") or ""
    if not raw.strip():
        return None
    try:
        items = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(items, list):
        return None
    for inst in items:
        if not isinstance(inst, dict) or not inst.get("enabled"):
            continue
        api_key = inst.get("api_key") or ""
        if "*" in str(api_key):
            # 掩码回显（前4位+星号+后4位，不以星号开头）：保留已保存的真实密钥
            inst = {**inst, "api_key": settings.llm_api_key}
        return inst
    return None


class LLMClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: float | None = None,
    ):
        settings = get_settings()
        # 多 LLM 实例配置：优先使用当前启用的实例（同一时刻仅一个 enabled），
        # 未配置实例时回退单实例 llm_* 字段；显式传参优先级最高
        inst = _active_llm_instance(settings)
        if inst is not None:
            self.base_url = (base_url or inst.get("base_url") or "").rstrip("/")
            self.api_key = api_key if api_key is not None else (inst.get("api_key") or "")
            self.model = model or inst.get("model") or ""
        else:
            self.base_url = (base_url or settings.llm_base_url).rstrip("/")
            self.api_key = api_key if api_key is not None else settings.llm_api_key
            self.model = model or settings.llm_model
        self.timeout = timeout or settings.llm_timeout_seconds
        self.max_tokens = int(getattr(settings, "llm_max_tokens", 16384) or 16384)
        # 关闭推理链：思考 token 与正文共享 max_tokens，结构化 JSON 任务里会把预算吃光。
        # 供应商不识别该字段时由 _post_with_thinking_fallback 记录到 _THINKING_UNSUPPORTED
        # 并自动降级，避免把兼容性问题暴露成解析失败。
        self.disable_thinking = bool(getattr(settings, "llm_disable_thinking", True))

    def _check_config(self) -> None:
        if not self.api_key:
            raise LLMError("llm_unconfigured", "语言模型未配置 API Key（DLAB_LLM_API_KEY）")
        if not self.base_url:
            raise LLMError("llm_unconfigured", "语言模型未配置 base_url")

    def _post(self, client: httpx.Client, payload: dict[str, Any], headers: dict[str, str]):
        """POST /chat/completions，附带关闭推理链的兼容降级。

        推理模型（deepseek-v4.x 等）默认输出思考链，思考 token 记在 completion 里，
        与正文共享 max_tokens。剧本解析这类长 JSON 任务里思考会把预算吃光，
        表现为 JSON 在字符串中途被截断（"Expecting ',' delimiter"），重试无效。
        这里显式传 thinking={"type": "disabled"}；若供应商不认该字段（HTTP 400
        且响应提到 thinking），本次去掉该字段重试，并记录到进程级黑名单，
        后续调用不再携带，保证对任意 OpenAI 兼容端点的兼容性。
        """
        url = f"{self.base_url}/chat/completions"
        want_disable = self.disable_thinking and self.base_url not in _THINKING_UNSUPPORTED
        if want_disable:
            payload = {**payload, "thinking": {"type": "disabled"}}
        resp = client.post(url, json=payload, headers=headers)
        if (
            want_disable
            and resp.status_code in (400, 422)
            and "thinking" in (resp.text or "").lower()
        ):
            _THINKING_UNSUPPORTED.add(self.base_url)
            payload = {k: v for k, v in payload.items() if k != "thinking"}
            resp = client.post(url, json=payload, headers=headers)
        return resp

    def chat(self, system_prompt: str, user_prompt: str, *, json_mode: bool = True,
             max_tokens: int | None = None) -> str:
        """单次对话调用，返回文本内容。json_mode 时请求 response_format=json_object。"""
        return self.chat_messages(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            json_mode=json_mode,
            max_tokens=max_tokens,
        )

    def chat_messages(self, messages: list[dict], *, json_mode: bool = False,
                      max_tokens: int | None = None) -> str:
        """多轮消息列表对话（导演台 Agent 用）：messages 为 OpenAI 格式 role/content 列表。
        默认 json_mode=False（自然语言回复）；错误分类与 chat() 一致。
        max_tokens 可单次覆盖实例默认值（长输出被截断时放大重试用）。"""
        self._check_config()
        limit = int(max_tokens or self.max_tokens)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": limit,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                resp = self._post(client, payload, headers)
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
            choice = data["choices"][0]
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise LLMError("llm_error", "语言模型响应结构异常") from e
        # 推理模型的思考 token 也占用 max_tokens；被截断时 JSON 可能仍「合法」但内容为空，
        # 必须显式失败，不能把残缺结果当成功落库。
        if choice.get("finish_reason") == "length":
            raise LLMError(
                "llm_output_truncated",
                f"语言模型输出被截断（max_tokens={limit}），请提高 llm_max_tokens 或缩小单次解析范围",
            )
        return content

    def chat_stream(self, system_prompt: str, user_prompt: str, *, json_mode: bool = True,
                    on_delta=None) -> str:
        """流式对话调用：SSE 增量接收，on_delta(text) 逐块回调，返回完整文本。
        网络错误分类与 chat() 一致；流中断视为 llm_error。"""
        self._check_config()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "text/event-stream"}
        parts: list[str] = []
        # 流式空闲超时：上游可能持续发送 SSE 心跳（注释行/空 data），
        # 使 httpx 的 read timeout 不断被重置，任务线程却永远收不到有效内容、无法结束。
        # 因此额外以『最后一次收到有效 delta 的时刻』为准做空闲判定。
        _settings = get_settings()
        idle_timeout = float(getattr(_settings, "llm_stream_idle_timeout_seconds", 60.0) or 60.0)
        last_content_at = time.monotonic()
        stream_timeout = httpx.Timeout(self.timeout, read=idle_timeout)
        try:
            with httpx.Client(timeout=stream_timeout, trust_env=False) as client:
                with client.stream("POST", f"{self.base_url}/chat/completions",
                                   json=payload, headers=headers) as resp:
                    if resp.status_code >= 400:
                        body = resp.read().decode(errors="replace")[:300]
                        raise LLMError("llm_error", f"语言模型接口 HTTP {resp.status_code}: {body}")
                    for line in resp.iter_lines():
                        # 空闲判定：连续 idle_timeout 秒未收到有效 delta 即判定超时，
                        # 避免只靠心跳维持连接时任务永久卡在 running
                        if time.monotonic() - last_content_at > idle_timeout:
                            raise LLMError(
                                "llm_timeout",
                                f"语言模型流式响应空闲超过 {idle_timeout:.0f} 秒"
                                f"（已接收 {sum(len(p) for p in parts)} 字符），判定为响应超时",
                            )
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data:
                            continue
                        if data == "[DONE]":
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        try:
                            delta = chunk["choices"][0]["delta"].get("content") or ""
                        except (KeyError, IndexError, TypeError):
                            continue
                        if delta:
                            parts.append(delta)
                            last_content_at = time.monotonic()
                            if on_delta is not None:
                                try:
                                    on_delta(delta)
                                except Exception:  # noqa: BLE001 - 回调异常不阻断流式接收
                                    pass
        except LLMError:
            raise
        except httpx.TimeoutException as e:
            raise LLMError("llm_timeout", f"语言模型请求超时: {type(e).__name__}") from e
        except httpx.ConnectError as e:
            raise LLMError("llm_unreachable", f"语言模型连接失败: {type(e).__name__}") from e
        except httpx.HTTPError as e:
            raise LLMError("llm_unreachable", f"语言模型网络错误: {type(e).__name__}") from e
        return "".join(parts)

    def chat_json(self, system_prompt: str, user_prompt: str,
                  *, max_tokens: int | None = None) -> dict:
        """单次对话调用并解析 JSON 输出。模型返回非合法 JSON 时报 llm_invalid_output。"""
        content = self.chat(system_prompt, user_prompt, json_mode=True, max_tokens=max_tokens)
        return parse_llm_json(content)


def is_truncated_json_output(message: str) -> bool:
    """判断 JSON 解析失败是否由输出被截断引起。

    上游不一定返回 finish_reason=length（部分代理会省略），此时只能从 JSON 解码
    错误特征判断：字符串未闭合、末尾缺分隔符/括号等。命中即值得放大 max_tokens 重试。
    """
    markers = (
        "Unterminated string",
        "Expecting ',' delimiter",
        "Expecting property name enclosed in double quotes",
        "Expecting value",
        "Invalid \\uXXXX escape",
    )
    return any(marker in (message or "") for marker in markers)


def parse_llm_json(content: str) -> dict:
    """解析 LLM 返回的 JSON 文本（兼容 markdown 代码块包裹与截断修复）。

    模型输出可能因 max_tokens 截断导致 JSON 不完整（字符串未闭合、括号不匹配等），
    此时先尝试启发式补齐再解析：补全末位缺失的引号与括号/方括号/花括号。
    """
    text = content.strip()
    # 兼容模型在 JSON 外包裹 markdown 代码块的情况
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines).strip()
    try:
        result = json.loads(text)
    except json.JSONDecodeError as e:
        repaired = _repair_truncated_json(text)
        if repaired != text:
            try:
                result = json.loads(repaired)
            except json.JSONDecodeError as e2:
                raise LLMError("llm_invalid_output", f"语言模型输出非合法 JSON: {e2}") from e2
        else:
            raise LLMError("llm_invalid_output", f"语言模型输出非合法 JSON: {e}") from e
    if not isinstance(result, dict):
        raise LLMError("llm_invalid_output", "语言模型输出 JSON 顶层须为对象")
    return result


def _repair_truncated_json(text: str) -> str:
    """启发式修复被截断的 JSON 文本。

    用栈跟踪括号打开顺序，按逆序补齐：先闭合内层再闭合外层。
    同时处理字符串内逗号缺失（行尾截断时常见）。
    """
    if not text:
        return text
    bracket_stack: list[str] = []
    in_str = False
    escaped = False
    i = 0
    while i < len(text):
        ch = text[i]
        if escaped:
            escaped = False
            i += 1
            continue
        if ch == "\\":
            escaped = True
            i += 1
            continue
        if ch == '"':
            in_str = not in_str
            i += 1
            continue
        if in_str:
            i += 1
            continue
        if ch == "{":
            bracket_stack.append("}")
        elif ch == "[":
            bracket_stack.append("]")
        elif ch in ("}", "]"):
            if bracket_stack and bracket_stack[-1] == ch:
                bracket_stack.pop()
        i += 1
    suffix = ""
    if in_str:
        suffix += '"'
    # 按逆序补齐：先闭合最后打开的内层括号
    while bracket_stack:
        suffix += bracket_stack.pop()
    return text + suffix if suffix else text
