"""ComfyUI 客户端：远端渲染底座的全部远程交互封装。

错误码映射（ARCHITECTURE.md §5.5 失败分类表）：
- 连接失败/超时 → comfy_unreachable / comfy_timeout
- 提交成功但本地未保存 → submission_unknown（由调用方处理）
- 产物缺失/类型不符 → output_missing / output_type_mismatch
纪律（AGENTS.md §3-4）：所有网络调用在短数据库事务之外执行。
"""
import json
from typing import Any

import httpx

from app.core.config import get_settings


class ComfyUIError(Exception):
    """ComfyUI 交互错误，携带失败分类错误码。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class ComfyUIClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None):
        settings = get_settings()
        self.base_url = (base_url or settings.comfyui_base_url).rstrip("/")
        self.timeout = timeout or settings.comfyui_timeout_seconds

    def _request(self, method: str, path: str, **kwargs) -> Any:
        url = f"{self.base_url}{path}"
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False) as client:
                resp = client.request(method, url, **kwargs)
        except httpx.TimeoutException as e:
            raise ComfyUIError("comfy_timeout", f"请求超时: {e}") from e
        except httpx.ConnectError as e:
            raise ComfyUIError("comfy_unreachable", f"连接失败: {e}") from e
        except httpx.HTTPError as e:
            raise ComfyUIError("comfy_unreachable", f"网络错误: {e}") from e
        if resp.status_code >= 400:
            raise ComfyUIError("comfy_execution_error", f"HTTP {resp.status_code}: {resp.text[:300]}")
        if resp.headers.get("content-type", "").startswith("application/json"):
            return resp.json()
        return resp.content

    def test_connection(self) -> dict:
        """连通性测试：拉取系统状态。"""
        return self._request("GET", "/system_stats")

    def submit_prompt(self, workflow: dict, client_id: str = "director-lab") -> str:
        """提交工作流，返回远端 prompt_id。"""
        payload = {"prompt": workflow, "client_id": client_id}
        result = self._request("POST", "/prompt", json=payload)
        prompt_id = result.get("prompt_id")
        if not prompt_id:
            raise ComfyUIError("comfy_execution_error", f"提交响应缺少 prompt_id: {result}")
        return prompt_id

    def get_history(self, prompt_id: str) -> dict | None:
        """查询任务历史。返回 None 表示任务尚未完成（仍在队列或执行中）。"""
        result = self._request("GET", f"/history/{prompt_id}")
        if not result:
            return None
        return result.get(prompt_id)

    def get_queue(self) -> dict:
        """查询当前队列状态。"""
        return self._request("GET", "/queue")

    def interrupt(self) -> None:
        """全局中断（会中断 ComfyUI 上所有正在执行的任务，调用方需二次确认）。"""
        self._request("POST", "/interrupt")

    def upload_image(self, file_path: str, filename: str | None = None, subfolder: str = "", overwrite: bool = True) -> str:
        """上传媒体文件，返回 ComfyUI 侧文件名。"""
        import os
        name = filename or os.path.basename(file_path)
        with open(file_path, "rb") as f:
            files = {"image": (name, f)}
            data = {"subfolder": subfolder, "overwrite": "true" if overwrite else "false"}
            result = self._request("POST", "/upload/image", files=files, data=data)
        comfy_name = result.get("name")
        if not comfy_name:
            raise ComfyUIError("comfy_upload_failed", f"上传响应缺少文件名: {result}")
        return comfy_name

    def download_file(self, filename: str, subfolder: str = "", file_type: str = "output") -> bytes:
        """下载产物文件内容。"""
        params = {"filename": filename, "subfolder": subfolder, "type": file_type}
        result = self._request("GET", "/view", params=params)
        if not isinstance(result, bytes):
            raise ComfyUIError("output_missing", f"产物下载失败: {filename}")
        return result

    def extract_outputs(self, history_entry: dict) -> dict:
        """从历史记录提取产物清单：节点 ID → 产物列表（images/videos/audio）。"""
        outputs = history_entry.get("outputs", {})
        result = {}
        for node_id, node_out in outputs.items():
            items = []
            for kind in ("images", "videos", "audio", "gifs"):
                for item in node_out.get(kind, []):
                    items.append({
                        "kind": kind.rstrip("s"),
                        "filename": item.get("filename"),
                        "subfolder": item.get("subfolder", ""),
                        "type": item.get("type", "output"),
                    })
            if items:
                result[node_id] = items
        return result

    def wait_for_job_ws(
        self,
        prompt_id: str,
        client_id: str = "director-lab",
        on_progress=None,
        poll_deadline: float | None = None,
    ) -> tuple[bool, str | None]:
        """通过 WebSocket 等待任务完成，返回 (是否完成, 失败错误码或 None)。

        - 订阅 ComfyUI 的 /ws?clientId=... 事件流，接收进度事件（progress/executing/executed）
        - on_progress(payload) 回调用于进度上报（如 {"value": 3, "max": 5, "node": "12"}）
        - WebSocket 异常或超时时降级为 HTTP 轮询 get_history，保证任务不因 WS 断连而丢失
        - poll_deadline 为 Unix 时间戳截止时刻；None 表示不设截止（由调用方控制）
        """
        import asyncio
        import time as _time

        ws_url = self.base_url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)

        async def _run() -> tuple[bool, str | None]:
            try:
                import websockets

                async with websockets.connect(
                    f"{ws_url}/ws?clientId={client_id}",
                    open_timeout=10,
                    close_timeout=5,
                    max_size=2**22,
                ) as ws:
                    while True:
                        if poll_deadline is not None and _time.time() >= poll_deadline:
                            return False, None
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                        except asyncio.TimeoutError:
                            continue
                        if isinstance(raw, bytes):
                            continue  # 预览帧二进制数据，跳过
                        try:
                            msg = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        mtype = msg.get("type", "")
                        data = msg.get("data", {}) or {}
                        if mtype == "progress" and on_progress:
                            # 同一 ComfyUI 实例上的所有任务共享同一条 /ws 事件流，
                            # progress 事件必须按 prompt_id 过滤；否则多任务并存时
                            # 排队任务会收到其它正在运行任务的进度，本地进度百分比失真。
                            _evt_pid = data.get("prompt_id")
                            if _evt_pid and _evt_pid != prompt_id:
                                continue
                            try:
                                on_progress({
                                    "value": data.get("value"),
                                    "max": data.get("max"),
                                    "node": data.get("node"),
                                })
                            except Exception:
                                pass
                        elif mtype == "executing":
                            # node=None 且 prompt_id 匹配表示该任务执行完毕
                            if data.get("prompt_id") == prompt_id and data.get("node") is None:
                                # 执行结束，通过 history 确认终态
                                return True, None
                        elif mtype == "execution_error":
                            if data.get("prompt_id") == prompt_id:
                                detail = json.dumps(data, ensure_ascii=False)[:300]
                                return True, f"comfy_execution_error:{detail}"
                        elif mtype == "execution_success":
                            if data.get("prompt_id") == prompt_id:
                                return True, None
            except Exception:
                # WebSocket 不可用：降级为 HTTP 轮询
                return False, None
            return False, None

        done, err = asyncio.run(_run())
        if done:
            return True, err
        # WS 未确认终态（断连/超时/无事件）：降级 HTTP 轮询确认
        history = self.get_history(prompt_id)
        if history is None:
            return False, None
        return self.is_job_done(history)

    def is_job_done(self, history_entry: dict) -> tuple[bool, str | None]:
        """判断历史记录是否为终态。返回 (是否完成, 失败错误码或 None)。"""
        status = history_entry.get("status", {})
        status_str = status.get("status_str", "")
        if status_str == "success":
            return True, None
        if status_str == "error":
            messages = status.get("messages", [])
            detail = json.dumps(messages, ensure_ascii=False)[:300] if messages else "未知错误"
            return True, f"comfy_execution_error:{detail}"
        return False, None

    def wait_for_job_http(
        self,
        prompt_id: str,
        on_progress=None,
        poll_interval: float = 3.0,
        poll_deadline: float | None = None,
    ) -> tuple[bool, str | None]:
        """纯 HTTP 轮询等待任务完成（不依赖 WebSocket），返回 (是否完成, 失败错误码或 None)。

        与 wait_for_job_ws 的区别：
        - 不使用 websockets 连接 /ws 事件流
        - 通过 GET /queue 获取队列中任务的实时进度（替代 WS progress 事件）
        - 通过 GET /history/{prompt_id} 轮询判断终态
        - 进度回调 on_progress(payload) 从 /queue 的 running 队列中提取

        Args:
            prompt_id: ComfyUI 任务 ID
            on_progress: 进度回调，payload 格式 {"value": int, "max": int, "node": str}
            poll_interval: 轮询间隔（秒），默认 3.0
            poll_deadline: Unix 时间戳截止时刻；None 表示不设截止（由调用方控制）

        Returns:
            (done: bool, error_code: str | None)
            - (True, None) 任务成功完成
            - (True, "comfy_execution_error:...") 任务失败
            - (False, None) 超时未完成（poll_deadline 到达）
        """
        import time as _time

        while True:
            if poll_deadline is not None and _time.time() >= poll_deadline:
                return False, None

            # 1. 轮询队列获取进度（HTTP GET /queue）
            if on_progress:
                try:
                    queue_data = self._request("GET", "/queue")
                    running_list = (queue_data or {}).get("queue_running", [])
                    for entry in running_list:
                        if not isinstance(entry, list) or len(entry) < 3:
                            continue
                        pid, _, task_info = entry[0], entry[1], entry[2]
                        if pid != prompt_id:
                            continue
                        if isinstance(task_info, dict):
                            step = task_info.get("step")
                            total = task_info.get("total")
                            if step is not None and total is not None:
                                try:
                                    on_progress({
                                        "value": int(step),
                                        "max": int(total),
                                        "node": "queue",
                                    })
                                except Exception:
                                    pass
                except ComfyUIError:
                    pass  # /queue 不可用不影响终态判断

            # 2. 轮询 history 判断终态
            try:
                history = self.get_history(prompt_id)
            except ComfyUIError:
                # 网络错误：等待重试
                _time.sleep(min(poll_interval, 2.0))
                continue

            if history is not None:
                done, err = self.is_job_done(history)
                if done:
                    return done, err
                # history 存在但未完成：退出信号（任务仍在执行）
                # 继续轮询

            # 未完成：等待后重试
            _time.sleep(poll_interval)
