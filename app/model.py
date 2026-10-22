"""Small, explicit adapters for public tool-calling model protocols.

No provider/model is silently chosen and no fallback fabricates a completion.
Retained Responses turns replay output items verbatim, including opaque reasoning.
Complete old turns may be retired into a visible-only checkpoint at a resolved
tool boundary; opaque reasoning is never included in application events/archive.
"""
from __future__ import annotations

import asyncio
import copy
import json
import math
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

import httpx

from .context import bounded_result, compact


class ModelError(RuntimeError):
    """Safe, user-visible model error (never embeds credentials or response bodies)."""


class ModelConfigurationError(ModelError):
    pass


class ModelProtocolError(ModelError):
    pass


class ModelBudgetExceeded(ModelError):
    pass


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    error: str | None = None


@dataclass
class ModelTurn:
    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)


@dataclass
class ModelSession:
    system: str
    history: list[dict[str, Any]] = field(default_factory=list)
    pending_calls: list[ToolCall] = field(default_factory=list)
    archive: list[dict[str, Any]] = field(default_factory=list)
    compactions: int = 0
    requests: list[dict[str, Any]] = field(default_factory=list)


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False)


class ModelClient:
    def __init__(
        self,
        config: dict[str, Any],
        before_request: Callable[[], Awaitable[None]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config
        self.protocol = config.get("model_protocol", "responses")
        if self.protocol not in {"responses", "chat_completions"}:
            raise ModelConfigurationError("model_protocol 必须为 responses 或 chat_completions")
        self.model = str(config.get("model_name") or "").strip()
        if not self.model:
            raise ModelConfigurationError("请先配置 model_name；应用不会默认假定任何模型权限")
        self.key_env = str(config.get("model_key_env") or "MODEL_API_KEY")
        self.key = os.environ.get(self.key_env, "")
        base_url = str(config.get("model_base_url") or "https://api.openai.com/v1").rstrip("/")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ModelConfigurationError("模型 base URL 必须为不含凭据、查询串或片段的 HTTP(S) 地址")
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not local:
            raise ModelConfigurationError("远程模型端点需要 HTTPS；本机模型可以使用 HTTP")
        if not self.key and not (local and config.get("model_allow_no_key", False)):
            raise ModelConfigurationError(f"缺少环境变量 {self.key_env}；请在后端环境配置密钥")
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        self.http = httpx.AsyncClient(
            base_url=base_url + "/", headers=headers,
            timeout=float(config.get("model_request_timeout_seconds", 90)),
            transport=transport, follow_redirects=False,
        )
        self.before_request = before_request
        self.requests: list[dict[str, Any]] = []

    def session(self, system: str, context: Any) -> ModelSession:
        session = ModelSession(system=system)
        budget = int(self.config.get("max_context_chars", 240_000))
        initial = bounded_result(session, context, max(1000, budget // 3))
        if initial is not context and isinstance(context, dict):
            # Keep account identity/version and the assigned problem immediately
            # available even if a caller supplies a large legacy material list.
            for key in ("task_id", "task", "account", "input_version", "scope", "reason"):
                if key in context:
                    initial[key] = copy.deepcopy(context[key])
        session.history.append({"role": "user", "content": encode(initial)})
        return session

    def add_user(self, session: ModelSession, message: Any) -> None:
        limit = min(int(self.config.get("max_tool_result_chars", 16000)), int(self.config.get("max_context_chars", 240_000)) // 4)
        session.history.append({"role": "user", "content": encode(bounded_result(session, message, max(500, limit)))})

    def add_results(self, session: ModelSession, results: list[tuple[ToolCall, Any]]) -> None:
        for call, result in results:
            limit = min(int(self.config.get("max_tool_result_chars", 16000)), int(self.config.get("max_context_chars", 240_000)) // 4)
            result = bounded_result(session, result, max(500, limit))
            if self.protocol == "responses":
                session.history.append({"type": "function_call_output", "call_id": call.id, "output": encode(result)})
            else:
                session.history.append({"role": "tool", "tool_call_id": call.id, "content": encode(result)})
            session.pending_calls = [pending for pending in session.pending_calls if pending.id != call.id]

    def resolve_pending(self, session: ModelSession, reason: str) -> None:
        """Make a cancelled/failed conversation resumable at a valid tool boundary."""
        self.add_results(session, [(call, {"failed": True, "error": reason}) for call in list(session.pending_calls)])

    async def aclose(self) -> None:
        await self.http.aclose()

    async def complete(self, session: ModelSession, tools: list[dict]) -> ModelTurn:
        compact(session, int(self.config.get("max_context_chars", 240_000)))
        if len(encode(session.history)) > int(self.config.get("max_context_chars", 240_000)):
            raise ModelProtocolError("初始账户或任务输入超过上下文上限；历史材料已有按需归档，请减少单次账户输入或检查 max_context_chars")
        history = [{key: copy.deepcopy(value) for key, value in item.items() if not key.startswith("_context_")} for item in session.history]
        if self.protocol == "responses":
            payload = {
                "model": self.model, "instructions": session.system,
                "input": history, "store": False,
                "max_output_tokens": int(self.config.get("max_output_tokens", 4000)),
                "tools": [{"type": "function", **tool, "strict": False} for tool in tools],
                "include": ["reasoning.encrypted_content"],
            }
            endpoint = "responses"
        else:
            payload = {
                "model": self.model,
                "messages": [{"role": "system", "content": session.system}, *history],
                "max_tokens": int(self.config.get("max_output_tokens", 4000)),
                "tools": [{"type": "function", "function": {**tool, "strict": False}} for tool in tools],
            }
            # Providers differ here. Explicit configuration replaces guesswork.
            if self.config.get("chat_use_max_completion_tokens", False):
                payload["max_completion_tokens"] = payload.pop("max_tokens")
            endpoint = "chat/completions"
        data = await self._post(endpoint, payload, session)
        if self.protocol == "responses":
            if data.get("status") in {"failed", "incomplete", "cancelled"}:
                raise ModelProtocolError(f"模型响应未完成（{data.get('status')}）；请检查输出预算或服务状态")
            output = data.get("output")
            if not isinstance(output, list) or not all(isinstance(item, dict) for item in output):
                raise ModelProtocolError("Responses 返回缺少 output 数组；端点可能不支持该协议")
            calls = [self._parse_call(item.get("call_id"), item.get("name"), item.get("arguments"))
                     for item in output if item.get("type") == "function_call"]
            parts = []
            for item in output:
                if item.get("type") != "message":
                    continue
                content = item.get("content", [])
                if not isinstance(content, list) or not all(isinstance(part, dict) for part in content):
                    raise ModelProtocolError("Responses 消息内容格式异常")
                parts.extend(part.get("text", "") for part in content if part.get("type") == "output_text")
            if not all(isinstance(part, str) for part in parts):
                raise ModelProtocolError("Responses 返回的文本类型异常")
            turn = ModelTurn("\n".join(parts), calls)
            # Do not filter opaque reasoning items out of continuation history.
            session.history.extend(copy.deepcopy(output))
        else:
            try:
                choice = data["choices"][0]
                message = choice["message"]
            except (KeyError, IndexError, TypeError) as exc:
                raise ModelProtocolError("Chat Completions 返回缺少 choices[0].message") from exc
            if not isinstance(choice, dict) or not isinstance(message, dict):
                raise ModelProtocolError("Chat Completions 消息格式异常")
            if choice.get("finish_reason") in {"length", "content_filter"}:
                raise ModelProtocolError("模型响应被截断或服务拒绝输出；未将截断结果当作完整分析")
            # Keep provider-specific continuation fields in memory, never emit them.
            continuation = copy.deepcopy(message)
            continuation["role"] = "assistant"
            raw_calls = message.get("tool_calls") or []
            if not isinstance(raw_calls, list) or not all(isinstance(call, dict) and isinstance(call.get("function"), dict) for call in raw_calls):
                raise ModelProtocolError("Chat Completions 工具调用格式异常")
            calls = [self._parse_call(call.get("id"), call.get("function", {}).get("name"),
                                      call.get("function", {}).get("arguments"))
                     for call in raw_calls]
            turn = ModelTurn(message.get("content") or "", calls)
            if not isinstance(turn.text, str):
                raise ModelProtocolError("Chat Completions 此适配器只接受文本回复")
            session.history.append(continuation)
        if len({call.id for call in turn.calls}) != len(turn.calls):
            raise ModelProtocolError("模型在同一轮返回重复工具 call ID，无法安全续接")
        if not turn.calls and not turn.text.strip():
            raise ModelProtocolError("模型返回空结果，未生成完整文本或本地函数调用")
        session.pending_calls = list(turn.calls)
        return turn

    @staticmethod
    def _parse_call(call_id: Any, name: Any, arguments: Any) -> ToolCall:
        if not isinstance(call_id, str) or not isinstance(name, str) or not call_id.strip() or not name.strip():
            raise ModelProtocolError("模型工具调用缺少有效 call ID 或工具名")
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
            if not isinstance(args, dict):
                raise ValueError("object required")
            json.dumps(args, allow_nan=False)
        except (ValueError, TypeError):
            return ToolCall(call_id, name, {}, "工具参数需要有效的 JSON 对象")
        return ToolCall(call_id, name, args)

    @staticmethod
    def usage_summary(requests: list[dict]) -> dict:
        usage = {}
        for request in requests:
            for key, value in (request.get("usage") or {}).items():
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
                    usage[key] = usage.get(key, 0) + value
        return {"requests": len(requests), "duration_ms": round(sum(r.get("duration_ms", 0) for r in requests), 2),
                "failed_requests": sum(bool(r.get("error")) for r in requests),
                "usage": usage or None, "usage_reported_requests": sum(r.get("usage") is not None for r in requests)}

    async def _post(self, endpoint: str, payload: dict, session: ModelSession | None = None) -> dict:
        retries = max(0, min(int(self.config.get("model_retries", 1)), 3))
        for attempt in range(retries + 1):
            if self.before_request:
                await self.before_request()
            started = time.perf_counter()
            observation = {"attempt": attempt + 1, "endpoint": endpoint, "model": self.model,
                           "status_code": None, "usage": None, "error": None}
            self.requests.append(observation)
            if session is not None:
                session.requests.append(observation)
            try:
                response = await self.http.post(endpoint, json=payload)
            except httpx.RequestError as exc:
                observation.update(duration_ms=round((time.perf_counter() - started) * 1000, 2), error=type(exc).__name__)
                if attempt < retries:
                    await asyncio.sleep(min(2 ** attempt, 4))
                    continue
                raise ModelError(f"模型网络请求失败（{type(exc).__name__}）；账户保存不受影响") from exc
            except asyncio.CancelledError:
                observation.update(duration_ms=round((time.perf_counter() - started) * 1000, 2), error="cancelled")
                raise
            observation.update(duration_ms=round((time.perf_counter() - started) * 1000, 2), status_code=response.status_code)
            if response.status_code >= 400:
                observation["error"] = f"HTTP {response.status_code}"
            if response.status_code in {429, 500, 502, 503, 504} and attempt < retries:
                await asyncio.sleep(min(2 ** attempt, 4))
                continue
            if response.status_code >= 400:
                raise ModelError(f"模型服务 HTTP {response.status_code}；请检查端点、权限、配额与协议配置")
            try:
                data = response.json()
            except ValueError as exc:
                observation["error"] = "invalid_json"
                raise ModelProtocolError("模型服务未返回 JSON") from exc
            if not isinstance(data, dict):
                observation["error"] = "invalid_response_type"
                raise ModelProtocolError("模型服务返回的 JSON 顶层必须为对象")
            if isinstance(data.get("usage"), dict):
                # Preserve only numeric usage counters, never raw provider fields.
                def counters(value):
                    return {k: counters(v) if isinstance(v, dict) else v for k, v in value.items()
                            if isinstance(v, dict) or (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v))}
                observation["usage"] = counters(data["usage"])
            return data
        raise ModelError("模型重试失败")


async def probe_model(config: dict[str, Any], *, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """Paid two-turn capability check; must be requested by the user/config UI.

    A locally generated nonce never appears until the tool result. Observing it
    in the second model response tests actual function-result continuation.
    This does not certify model research quality or every optional API feature.
    """
    client = ModelClient(config, transport=transport)
    nonce = secrets.token_hex(12)
    tool = {"name": "capability_echo", "description": "Call once to retrieve a generated token.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}
    session = client.session("Call capability_echo once. After its result, return exactly its token, with no other text.", "Check function result continuation.")
    try:
        first = await client.complete(session, [tool])
        if len(first.calls) != 1 or first.calls[0].name != "capability_echo" or first.calls[0].error:
            raise ModelProtocolError("能力检查失败：模型没有按要求调用函数")
        client.add_results(session, [(first.calls[0], {"token": nonce})])
        second = await client.complete(session, [tool])
        if second.calls or nonce not in second.text:
            raise ModelProtocolError("能力检查失败：模型未正确续接函数返回值")
        return {"ok": True, "protocol": client.protocol, "model": client.model,
                "checks": ["function_call", "function_result_continuation"],
                "model_usage": client.usage_summary(client.requests),
                "requests": copy.deepcopy(client.requests),
                "note": "已实测此端点和模型的两轮工具协议；不等于研究质量验收。"}
    finally:
        await client.aclose()
