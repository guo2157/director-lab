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
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.config import get_settings
from app.integrations.llm_response import (
    ERR_EMPTY_CONTENT,
    ERR_INVALID_OUTPUT,
    ERR_OUTPUT_TRUNCATED,
    ERR_STREAM_INTERRUPTED,
    LLMResponseMeta,
    ValidationResult,
    is_likely_truncated_json,
    note_provider_cap,
    parse_json_safely,
    validate_llm_response,
)


def _raise_from_validation(result: ValidationResult, message: str | None = None,
                           *, meta: LLMResponseMeta | None = None) -> None:
    """把校验结果转成 LLMError（错误码透传，供重试策略按类型决策）。"""
    raise LLMError(result.error_code or ERR_INVALID_OUTPUT,
                   message or result.error_message or "语言模型响应校验未通过",
                   meta=meta)


def _coerce_content(raw: Any) -> str | None:
    """把 message.content 归一化为纯文本。

    OpenAI 标准是字符串；部分供应商返回内容块数组
    ``[{"type": "text", "text": "..."}]``。两种都要支持，
    否则会把整段正文丢掉、表现为「content 为空」。
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
                elif isinstance(item.get("content"), str):
                    parts.append(item["content"])
        return "".join(parts)
    if isinstance(raw, dict):
        text = raw.get("text") or raw.get("content")
        return text if isinstance(text, str) else None
    return str(raw)


@dataclass
class LLMCallMetadata:
    """单次 LLM 调用的元数据（用于观测、统计与重试决策）。"""
    model: str = ""
    max_tokens: int = 0
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    reasoning_tokens: int | None = None
    duration_ms: int = 0
    retry_count: int = 0

# 已确认不支持 thinking 字段的端点（base_url）：命中后不再携带该字段，保证兼容性
_THINKING_UNSUPPORTED: set[str] = set()


class LLMError(Exception):
    """语言模型交互错误，携带失败分类错误码（message 不含凭据）。

    附加元数据（content_length / reasoning_length / usage / finish_reason）用于
    重试记录与问题复盘：空输出时「思考吃了多少 token」是判断该不该放大预算的
    关键依据，不能丢。
    """

    def __init__(self, code: str, message: str, *, meta: "LLMResponseMeta | None" = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.content_length = meta.content_length if meta else 0
        self.reasoning_length = meta.reasoning_length if meta else 0
        self.finish_reason = meta.finish_reason if meta else None
        self.usage = dict(meta.usage or {}) if meta else {}
        self.request_id = meta.request_id if meta else None


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
        self.max_tokens = int(getattr(settings, "llm_max_tokens", 32768) or 32768)
        # 保留推理能力：剧本解析依赖模型的深度推理（角色关系、剧情因果、场景边界等），
        # 默认不禁用 Thinking。仅当 llm_disable_thinking=True 时显式发送 thinking.type=disabled。
        # 供应商不识别该字段时由 _post 记录到 _THINKING_UNSUPPORTED 并自动降级。
        self.disable_thinking = bool(getattr(settings, "llm_disable_thinking", False))

    def _check_config(self) -> None:
        if not self.api_key:
            raise LLMError("llm_unconfigured", "语言模型未配置 API Key（DLAB_LLM_API_KEY）")
        if not self.base_url:
            raise LLMError("llm_unconfigured", "语言模型未配置 base_url")

    def _thinking_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        """根据 disable_thinking 配置决定是否注入 thinking 参数。
        仅当 disable_thinking=True 且端点不在黑名单时才注入 thinking.type=disabled。
        disable_thinking=False（默认）时不传 thinking 字段，保留模型推理能力。
        """
        if self.disable_thinking and self.base_url not in _THINKING_UNSUPPORTED:
            return {**payload, "thinking": {"type": "disabled"}}
        return payload

    def _post(self, client: httpx.Client, payload: dict[str, Any], headers: dict[str, str]):
        """POST /chat/completions，附带 Thinking 配置的兼容降级。

        Thinking 策略由 llm_disable_thinking 控制（默认 False，保留推理）。
        若配置为关闭且端点不识别 thinking 字段（HTTP 400/422 含 thinking），
        则自动移除该字段并记录到进程级黑名单，后续调用不再携带，
        保证对任意 OpenAI 兼容端点的兼容性。
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
        max_tokens 可单次覆盖实例默认值（长输出被截断时放大重试用）。

        响应完整性统一由 ``validate_llm_response`` 判定：空正文、截断、供应商错误
        各有独立错误码，不再让空串直接落到 json.loads 变成
        ``Expecting value: line 1 column 1 (char 0)``。
        """
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
            meta = LLMResponseMeta(
                content=None, provider_error=f"HTTP {resp.status_code}",
                model=self.model, max_tokens=limit,
                request_id=resp.headers.get("x-request-id"),
            )
            _raise_from_validation(validate_llm_response(meta),
                                   f"语言模型接口 HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        meta = self._extract_completion_meta(data, limit=limit)
        # 推理模型的思考 token 也占用 max_tokens；被截断时空正文或 JSON 残缺，
        # 必须显式失败，不能把残缺结果当成功落库。
        result = validate_llm_response(meta)
        if not result.valid:
            _raise_from_validation(result)
        # 记录该端点观测到的 completion 上限：网关可能静默截断并谎报 stop，
        # 只有靠 usage 才能识别（见 llm_response.detect_truncation）
        note_provider_cap(self.base_url, meta.completion_tokens)
        return meta.content or ""

    def _extract_completion_meta(self, data: dict, *, limit: int) -> LLMResponseMeta:
        """从非流式响应体提取完整元数据（content / reasoning / finish_reason / usage）。

        兼容两种结构：``message.content`` 为字符串（标准）或为内容块数组（部分供应商）。
        """
        try:
            choices = data.get("choices") or []
            choice = choices[0] if choices else {}
            message = choice.get("message") or {}
            return LLMResponseMeta(
                content=_coerce_content(message.get("content")),
                reasoning_content=(message.get("reasoning_content")
                                   or message.get("reasoning")),
                finish_reason=choice.get("finish_reason"),
                usage=data.get("usage") or {},
                request_id=data.get("id"),
                stream_completed=True,
                model=str(data.get("model") or self.model),
                max_tokens=limit,
            )
        except (AttributeError, TypeError):
            return LLMResponseMeta(
                content=None, provider_error="响应结构异常",
                model=self.model, max_tokens=limit,
            )

    def chat_stream(self, system_prompt: str, user_prompt: str, *, json_mode: bool = True,
                    on_delta=None, max_tokens: int | None = None) -> str:
        """流式对话调用：SSE 增量接收，on_delta(text) 逐块回调，返回完整正文。

        收集纪律（本次修复重点）：
        - content 与 reasoning_content **分别**收集，reasoning 绝不作为最终 JSON；
        - 某个 chunk 的 content 为空不退出（推理模型前 N 个 chunk 只有思考内容）；
        - 记录最后一个非空 finish_reason 与 usage（含 reasoning_tokens）；
        - 只有收到 ``[DONE]`` 才算正常完成，否则判定 llm_stream_interrupted；
        - 后续空 chunk 不会覆盖已收集内容（只 append）。
        调用后可从 self.last_metadata 获取本次调用的结构化元数据。
        """
        self._check_config()
        limit = int(max_tokens or self.max_tokens)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_tokens": limit,
            "stream": True,
            # 要求供应商在流末尾补一个 usage chunk（不支持的供应商会忽略该字段）
            "stream_options": {"include_usage": True},
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        # 流式也走 Thinking 兼容策略（与 _post 一致的逻辑）
        payload = self._thinking_payload(payload)
        headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "text/event-stream"}
        parts: list[str] = []
        reasoning_parts: list[str] = []
        last_finish_reason: str | None = None
        last_usage: dict | None = None
        request_id: str | None = None
        stream_completed = False
        # 流式空闲超时
        _settings = get_settings()
        idle_timeout = float(getattr(_settings, "llm_stream_idle_timeout_seconds", 60.0) or 60.0)
        last_content_at = time.monotonic()
        stream_timeout = httpx.Timeout(self.timeout, read=idle_timeout)
        try:
            with httpx.Client(timeout=stream_timeout, trust_env=False) as client:
                with client.stream("POST", f"{self.base_url}/chat/completions",
                                   json=payload, headers=headers) as resp:
                    request_id = resp.headers.get("x-request-id")
                    if resp.status_code >= 400:
                        body = resp.read().decode(errors="replace")[:300]
                        meta = LLMResponseMeta(
                            content=None, provider_error=f"HTTP {resp.status_code}",
                            model=self.model, max_tokens=limit,
                            request_id=request_id, stream_completed=False,
                        )
                        _raise_from_validation(
                            validate_llm_response(meta),
                            f"语言模型接口 HTTP {resp.status_code}: {body}",
                        )
                    for line in resp.iter_lines():
                        if time.monotonic() - last_content_at > idle_timeout:
                            raise LLMError(
                                "llm_timeout",
                                f"语言模型流式响应空闲超过 {idle_timeout:.0f} 秒"
                                f"（已接收 {sum(len(p) for p in parts)} 字符正文），"
                                "判定为响应超时",
                            )
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data:
                            continue
                        if data == "[DONE]":
                            stream_completed = True
                            break
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        # usage 可能出现在独立的末尾 chunk（choices 为空）
                        chunk_usage = chunk.get("usage")
                        if isinstance(chunk_usage, dict) and chunk_usage:
                            last_usage = {
                                "prompt_tokens": chunk_usage.get("prompt_tokens"),
                                "completion_tokens": chunk_usage.get("completion_tokens"),
                                "total_tokens": chunk_usage.get("total_tokens"),
                                "reasoning_tokens": (
                                    chunk_usage.get("reasoning_tokens")
                                    or (chunk_usage.get("completion_tokens_details") or {})
                                    .get("reasoning_tokens")
                                ),
                            }
                        choices = chunk.get("choices") or []
                        if not choices:
                            # 纯 usage chunk / 心跳：不退出，继续消费
                            continue
                        try:
                            choice0 = choices[0]
                        except (KeyError, IndexError, TypeError):
                            continue
                        delta_obj = choice0.get("delta") or {}
                        # content 与 reasoning 分别收集，互不覆盖
                        delta = delta_obj.get("content") or ""
                        reasoning = delta_obj.get("reasoning_content") or ""
                        if reasoning:
                            reasoning_parts.append(reasoning)
                            last_content_at = time.monotonic()
                        # 记录最后一个非空 finish_reason
                        fr = choice0.get("finish_reason")
                        if fr:
                            last_finish_reason = str(fr)
                        if delta:
                            parts.append(delta)
                            last_content_at = time.monotonic()
                            if on_delta is not None:
                                try:
                                    on_delta(delta)
                                except Exception:  # noqa: BLE001
                                    pass
        except LLMError:
            raise
        except httpx.TimeoutException as e:
            raise LLMError("llm_timeout", f"语言模型请求超时: {type(e).__name__}") from e
        except httpx.ConnectError as e:
            raise LLMError("llm_unreachable", f"语言模型连接失败: {type(e).__name__}") from e
        except httpx.HTTPError as e:
            raise LLMError("llm_unreachable", f"语言模型网络错误: {type(e).__name__}") from e

        content = "".join(parts)
        reasoning = "".join(reasoning_parts) if reasoning_parts else None
        # 将本次调用的元数据存入实例属性，供调用方观测与统计
        self.last_metadata = LLMCallMetadata(
            model=self.model,
            max_tokens=limit,
            finish_reason=last_finish_reason,
            prompt_tokens=last_usage.get("prompt_tokens") if last_usage else None,
            completion_tokens=last_usage.get("completion_tokens") if last_usage else None,
            reasoning_tokens=last_usage.get("reasoning_tokens") if last_usage else None,
            retry_count=self.last_metadata.retry_count if hasattr(self, "last_metadata") else 0,
        )
        # 统一完整性校验：空正文 / 截断 / 流中断 各有独立错误码
        meta = LLMResponseMeta(
            content=content,
            reasoning_content=reasoning,
            finish_reason=last_finish_reason,
            usage=last_usage or {},
            request_id=request_id,
            stream_completed=stream_completed,
            model=self.model,
            max_tokens=limit,
        )
        result = validate_llm_response(meta)
        if not result.valid:
            _raise_from_validation(result)
        # 记录该端点观测到的 completion 上限（同 chat_messages）
        note_provider_cap(self.base_url, meta.completion_tokens)
        return content

    def chat_json(self, system_prompt: str, user_prompt: str,
                  *, max_tokens: int | None = None) -> dict:
        """单次对话调用并解析 JSON 输出。

        与旧实现的差异：把本次调用的元数据（finish_reason / usage）一并传给
        解析器，使「供应商静默截断」也能被判为 llm_output_truncated，
        而不是笼统的 llm_invalid_output。
        """
        content = self.chat(system_prompt, user_prompt, json_mode=True, max_tokens=max_tokens)
        m = getattr(self, "last_metadata", None)
        meta = LLMResponseMeta(
            content=content,
            finish_reason=getattr(m, "finish_reason", None),
            usage={
                "completion_tokens": getattr(m, "completion_tokens", None),
                "reasoning_tokens": getattr(m, "reasoning_tokens", None),
                "prompt_tokens": getattr(m, "prompt_tokens", None),
            },
            model=self.model,
            max_tokens=int(max_tokens or self.max_tokens),
        )
        obj, result = parse_json_safely(content, expect=dict,
                                        meta=meta, base_url=self.base_url)
        if obj is None:
            # meta 必须透传：否则重试记录里的 content_length 会是 0，
            # 「正文拿到了但 JSON 不合法」与「正文根本没拿到」将无法区分。
            raise LLMError(result.error_code or ERR_INVALID_OUTPUT,
                           result.error_message or "语言模型输出无法解析为 JSON",
                           meta=meta)
        return obj


def is_truncated_json_output(message: str, content: str | None = None) -> bool:
    """判断 JSON 解析失败是否由输出被截断引起（委托 llm_response 单一实现）。

    与旧实现的区别：不再把 ``Expecting value`` 当截断证据——空串、纯空白、
    纯自然语言都会抛它，把它一律当截断正是本次误判的根源。
    """
    return is_likely_truncated_json(message, content)


def parse_llm_json(content: str) -> dict:
    """解析 LLM 返回的 JSON 文本（委托 llm_response.parse_json_safely）。

    空内容返回 ``llm_empty_content``（而非笼统的 llm_invalid_output）；
    ``Expecting value`` 不再被判为截断；markdown 代码块、前后夹文本、
    真正的截断各有独立处理分支。

    注意：**截断不补齐**。补齐后的 JSON 虽然语法合法，但会缺字段
    （例如只写了一半的 appearance），一旦落库就是残缺数据。这里宁可抛
    ``llm_output_truncated`` 让上层放大 max_tokens 重试。
    """
    meta = LLMResponseMeta(content=content)
    obj, result = parse_json_safely(content, expect=dict, meta=meta)
    if obj is None:
        raise LLMError(result.error_code or ERR_INVALID_OUTPUT,
                       result.error_message or "语言模型输出无法解析为 JSON",
                       meta=meta)
    return obj
