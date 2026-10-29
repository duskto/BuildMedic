import asyncio
import json
import time
from urllib.parse import urlsplit

import httpx

from .config import settings
from .errors import AppError, Blocked
from .model_providers import OFFICIAL_ENDPOINTS, completion_parameters, kimi_always_thinks, tools_supported
from .schemas import NativeAssistant
from .security import sanitize


class RunnerClient:
    def __init__(self):
        cfg = settings()
        self.client = httpx.AsyncClient(
            base_url=cfg.runner_url,
            headers={"Authorization": "Bearer " + cfg.runner_token},
            timeout=15,
            trust_env=False,
        )

    async def close(self):
        await self.client.aclose()

    async def health(self):
        if not settings().runner_token:
            raise Blocked("未配置内部 Runner 令牌，请设置 BUILDMEDIC_RUNNER_TOKEN")
        try:
            response = await self.client.get("/runner/v1/health")
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise Blocked("无法连接受信任的 Runner 服务，请检查地址、令牌与进程状态") from exc
        if not data["ready"]:
            raise Blocked(data["message"])
        return data

    async def submit(self, execution_id, case_id, check_id, patch):
        response = await self.client.post(
            "/runner/v1/executions",
            json={"execution_id": execution_id, "case_id": case_id, "check_id": check_id, "patch": patch},
        )
        response.raise_for_status()
        return response.json()

    async def prepare(self, binding):
        try:
            response = await self.client.post("/runner/v1/snapshots", json=binding, timeout=60)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise Blocked("Runner 无法准备登记快照，请检查 profile、固定镜像与归档指纹") from exc

    async def submit_remote(self, execution_id, binding, check_id, patch):
        try:
            response = await self.client.post(
                "/runner/v1/executions",
                json={
                    "execution_id": execution_id,
                    "remote": binding,
                    "check_id": check_id,
                    "patch": patch,
                },
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPError as exc:
            raise Blocked("Runner 拒绝远程执行，请检查已登记 profile 与固定快照") from exc

    async def get(self, execution_id):
        response = await self.client.get(f"/runner/v1/executions/{execution_id}")
        response.raise_for_status()
        return response.json()

    async def cancel(self, execution_id):
        response = await self.client.post(f"/runner/v1/executions/{execution_id}/cancel")
        response.raise_for_status()
        for _ in range(30):
            result = await self.get(execution_id)
            if result["lifecycle"] in {"completed", "cancelled", "interrupted"}:
                return result.get("termination", {}).get("cleanup_confirmed") is not False
            await asyncio.sleep(0.1)
        return False


class ModelClient:
    """Bounded Chat Completions transport with explicit provider configuration."""

    def __init__(self, transport=None):
        self.client = httpx.AsyncClient(
            timeout=settings().model_timeout_seconds, follow_redirects=False, transport=transport
        )
        self.metadata = {}

    async def close(self):
        await self.client.aclose()

    def configuration(self):
        cfg = settings()
        if not cfg.model_ready:
            raise Blocked("未配置真实模型服务，请设置 MODEL_BASE_URL、MODEL_API_KEY 和 MODEL_ID")
        parsed = urlsplit(cfg.model_base_url)
        try:
            port = parsed.port
        except ValueError as exc:
            raise AppError("model_configuration", "模型 API 地址端口无效", 422) from exc
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise AppError("model_configuration", "模型 API 地址不能包含凭据、查询参数或片段", 422)
        if cfg.model_endpoint_mode == "official" and cfg.model_provider in OFFICIAL_ENDPOINTS:
            if (
                parsed.scheme != "https"
                or port not in (None, 443)
                or (parsed.hostname, parsed.path.rstrip("/")) not in OFFICIAL_ENDPOINTS[cfg.model_provider]
            ):
                raise AppError(
                    "model_configuration", "所选服务商与官方 API 根地址不匹配，请参照模型接入说明", 422
                )
        elif parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise AppError("model_configuration", "模型服务须使用 HTTPS，本机开发地址可使用 HTTP", 422)
        if (
            cfg.model_provider == "kimi"
            and kimi_always_thinks(cfg.model_id)
            and cfg.model_thinking != "enabled"
        ):
            raise AppError(
                "model_configuration",
                "所选 Kimi 模型始终思考，请设 MODEL_THINKING=enabled 或换用可关闭思考的模型",
                422,
            )
        return cfg

    def check_status(self, response):
        status = response.status_code
        if 200 <= status < 300:
            return
        if response.is_redirect:
            raise AppError("model_redirect", "模型服务发生重定向，已停止发送凭据", 502)
        code, message = {
            401: ("model_authentication", "模型密钥无效或已失效，请更新服务端密钥"),
            402: ("model_insufficient_balance", "模型账户余额不足，请充值后重新诊断"),
            403: ("model_forbidden", "模型服务拒绝访问，请检查账户权限"),
            404: ("model_not_found", "模型或 API 路径不存在，请检查服务端配置"),
            429: ("model_http_error", "模型服务限流，请稍后重试"),
        }.get(status, ("model_http_error", f"模型服务返回 HTTP {status}"))
        retry_after = response.headers.get("retry-after")
        if retry_after is not None and not retry_after.isdecimal():
            retry_after = None
        raise AppError(
            code,
            message,
            502,
            status in {408, 429, 500, 502, 503, 504},
            {"provider_status": status, "retry_after_seconds": int(retry_after) if retry_after else None},
        )

    async def request_json(self, method, path, body=None):
        cfg = self.configuration()
        try:
            async with self.client.stream(
                method,
                cfg.model_base_url.rstrip("/") + path,
                headers={"Authorization": "Bearer " + cfg.model_api_key},
                **({"json": body} if body is not None else {}),
            ) as response:
                self.check_status(response)
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 500_000:
                        raise AppError("model_output_too_large", "模型响应超出大小限制", 502)
                    chunks.append(chunk)
                return json.loads(b"".join(chunks))
        except httpx.TimeoutException as exc:
            raise AppError("model_timeout", "模型调用超时，已保留现有执行证据", 504, True) from exc
        except httpx.RequestError as exc:
            raise AppError("model_connection", "无法连接模型服务，请检查后端网络或代理", 503, True) from exc
        except (ValueError, TypeError) as exc:
            raise AppError("model_format_error", "模型服务响应格式不合法", 502) from exc

    async def probe(self):
        """Read model availability where supported; never trigger a paid completion."""
        cfg = self.configuration()
        if cfg.model_provider == "glm" and cfg.model_endpoint_mode == "official":
            return self.unverified_probe(cfg)
        try:
            data = await self.request_json("GET", "/models")
        except AppError as exc:
            if cfg.model_endpoint_mode == "relay" and exc.details.get("provider_status") in {404, 405, 501}:
                return self.unverified_probe(cfg)
            raise
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise AppError("model_format_error", "模型列表响应格式不合法", 502)
        ids = {m.get("id") for m in data["data"] if isinstance(m, dict) and isinstance(m.get("id"), str)}
        if cfg.model_id not in ids:
            raise AppError("model_not_found", "当前账户的模型列表不包含所配置的模型，请更新 MODEL_ID", 422)
        return {
            "provider": cfg.model_provider,
            "model_id": cfg.model_id,
            "status": "available",
            "checked_at": time.time(),
            "message": "鉴权成功，所选模型可用；尚未验证余额或实际生成能力",
        }

    async def complete(self, messages):
        message, usage = await self._chat(messages)
        content = message.get("content")
        if not isinstance(content, str):
            raise AppError("model_format_error", "模型服务响应格式不合法", 502)
        if not content.strip():
            raise AppError("model_empty_response", "模型返回空内容，请重新诊断", 502, True)
        try:
            content.encode("utf-8")
        except UnicodeError as exc:
            raise AppError("model_format_error", "模型返回的文本编码无效", 502) from exc
        return content, usage

    @staticmethod
    def unverified_probe(cfg):
        return {
            "provider": cfg.model_provider,
            "model_id": cfg.model_id,
            "status": "unverified",
            "checked_at": time.time(),
            "message": "当前接口不支持模型列表检查；配置格式有效，尚未验证密钥或生成能力。"
            "可在服务端运行 check_model.py --generate 进行一次付费检查。",
        }

    async def tool_turn(self, messages, tools):
        cfg = self.configuration()
        if not tools_supported(cfg):
            raise Blocked("当前模型或思考配置不支持工具诊断；请选择可关闭思考的模型并关闭思考模式")
        message, usage = await self._chat(messages, tools=tools)
        try:
            calls = message.get("tool_calls") or []
            if not isinstance(calls, list) or len(calls) > 8:
                raise ValueError("Invalid tool batch")
            frame = NativeAssistant.model_validate(
                {
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": call["type"],
                            "function": {
                                "name": call["function"]["name"],
                                "arguments": call["function"]["arguments"],
                            },
                        }
                        for call in calls
                    ]
                }
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise AppError("model_format_error", "模型工具响应结构无效，未执行工具", 502) from exc
        return frame.model_dump(), usage

    async def _chat(self, messages, tools=None):
        cfg = self.configuration()
        self.metadata = {}
        body = {
            "model": cfg.model_id,
            "messages": messages,
            **completion_parameters(cfg),
        }
        if tools is None:
            body["response_format"] = {"type": "json_object"}
        else:
            body.update(tools=tools, tool_choice="auto")
        data = await self.request_json("POST", "/chat/completions", body)
        try:
            choice = data["choices"][0]
            self.metadata = {
                key: value
                for key, value in {
                    "response_model": data.get("model"),
                    "response_id": data.get("id"),
                    "finish_reason": choice.get("finish_reason"),
                }.items()
                if isinstance(value, str)
                and len(value) <= 200
                and value.isascii()
                and sanitize(value) == value
            }
            if choice.get("finish_reason") == "length":
                raise AppError(
                    "model_output_truncated", "模型输出被截断，请提高输出上限或关闭思考模式后重试", 502
                )
            if choice.get("finish_reason") in {"content_filter", "insufficient_system_resource", "aborted"}:
                raise AppError("model_generation_stopped", "模型服务未完成生成，请稍后重试", 502, True)
            message = choice["message"]
            if not isinstance(message, dict):
                raise TypeError("Invalid message")
            return message, data.get("usage")
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            raise AppError("model_format_error", "模型服务响应格式不合法", 502) from exc
