"""Small, explicit adapters for public tool-calling model protocols.

No provider/model is silently chosen and no fallback fabricates a completion.
Retained Responses turns replay output items verbatim, including opaque reasoning.
Complete old turns may be retired into a visible-only checkpoint at a resolved
tool boundary; opaque reasoning is never included in application events/archive.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import math
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

from .context import bounded_result, compact
from .config import reasoning_effort, model_config_summary


class ModelError(RuntimeError):
    """Safe, user-visible model error (never embeds credentials or response bodies)."""


class ModelConfigurationError(ModelError):
    pass


class ReasoningEffortUnsupported(ModelConfigurationError):
    """A deterministic endpoint rejection; never retry with a different effort."""


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
    native_events: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    annotations: list[dict] = field(default_factory=list)
    continuation_required: bool = False
    native_status: dict = field(default_factory=dict)


@dataclass
class ModelSession:
    system: str
    history: list[dict[str, Any]] = field(default_factory=list)
    pending_calls: list[ToolCall] = field(default_factory=list)
    archive: list[dict[str, Any]] = field(default_factory=list)
    compactions: int = 0
    requests: list[dict[str, Any]] = field(default_factory=list)
    agent_id: str = "unknown"
    phase: str = "research"
    native_search: bool = True
    task_id: str | None = None


_NATIVE_CAPABILITIES: dict[str, dict] = {}


def native_search_fingerprint(config: dict) -> str:
    """Invalidate evidence on endpoint/model/credential/effort changes, without a key."""
    identity = [str(config.get(k, "")) for k in ("model_protocol", "model_base_url", "model_name", "model_key_env")]
    identity.append(config.get("_credential_fingerprint") or hashlib.sha256(str(config.get("_model_api_key", os.environ.get(config.get("model_key_env", "MODEL_API_KEY"), ""))).encode()).hexdigest())
    identity.append(model_config_summary(config)["fingerprint"])
    return hashlib.sha256(json.dumps(identity).encode()).hexdigest()


def native_search_status(config: dict, cached: dict | None = None) -> dict:
    fingerprint = native_search_fingerprint(config)
    if not config.get("model_native_search", True):
        return {"status": "disabled", "fingerprint": fingerprint, "note": "用户已关闭模型原生联网。"}
    if config.get("model_protocol", "responses") != "responses":
        return {"status": "unsupported", "fingerprint": fingerprint,
                "note": "当前仅适配 Responses 内置 web_search；Chat Completions 的独立搜索模型不属于此路径。"}
    prior = cached or _NATIVE_CAPABILITIES.get(fingerprint)
    if prior and prior.get("fingerprint") == fingerprint:
        return copy.deepcopy(prior)
    return {"status": "unverified", "fingerprint": fingerprint,
            "note": "尚未观察到此端点、模型与凭据的原生搜索执行；接受 tools 参数不构成成功。"}


def _numeric_counters(value: dict) -> dict:
    return {k: _numeric_counters(v) if isinstance(v, dict) else v for k, v in value.items()
            if isinstance(v, dict) or (isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v))}


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, allow_nan=False)


class ModelClient:
    def __init__(
        self,
        config: dict[str, Any],
        before_request: Callable[[], Awaitable[None]] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.config = config = copy.deepcopy(config)
        try:
            self.reasoning_effort = reasoning_effort(config)
        except ValueError as exc:
            raise ModelConfigurationError(str(exc)) from exc
        if 'reasoning' in config and not isinstance(config['reasoning'], dict):
            raise ModelConfigurationError('Responses reasoning 配置必须为对象')
        self.protocol = config.get("model_protocol", "responses")
        if self.protocol not in {"responses", "chat_completions"}:
            raise ModelConfigurationError("model_protocol 必须为 responses 或 chat_completions")
        self.model = str(config.get("model_name") or "").strip()
        if not self.model:
            raise ModelConfigurationError("请先配置 model_name；应用不会默认假定任何模型权限")
        self.key_env = str(config.get("model_key_env") or "MODEL_API_KEY")
        self.key = config.get("_model_api_key", os.environ.get(self.key_env, ""))
        base_url = str(config.get("model_base_url") or "https://api.openai.com/v1").rstrip("/")
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ModelConfigurationError("模型 base URL 必须为不含凭据、查询串或片段的 HTTP(S) 地址")
        local = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not local:
            raise ModelConfigurationError("远程模型端点需要 HTTPS；本机模型可以使用 HTTP")
        if not self.key and not (local and config.get("model_allow_no_key", False)):
            raise ModelConfigurationError(f"缺少环境变量 {self.key_env}；请在后端环境配置密钥")
        self.base_url = base_url
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
        session.task_id = (context.get("task_id") or context.get("job_id")) if isinstance(context, dict) else None
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

    def native_status(self) -> dict:
        return native_search_status(self.config)

    def _native_observation(self, status: str, **details: Any) -> dict:
        result = {"status": status, "fingerprint": native_search_fingerprint(self.config),
                  "checked_at": datetime.now(timezone.utc).isoformat(), **details}
        _NATIVE_CAPABILITIES[result["fingerprint"]] = result
        return copy.deepcopy(result)

    async def complete(self, session: ModelSession, tools: list[dict], *, tool_choice: str | None = None) -> ModelTurn:
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
                "reasoning": {**copy.deepcopy(self.config.get("reasoning") or {}), "effort": self.reasoning_effort},
            }
            if self.config.get("model_native_search", True) and session.native_search:
                # The documented Responses boundary shared by OpenAI and xAI.
                # Compatibility endpoints remain unverified until they return
                # observable execution; unsupported requests never fall back.
                payload["tools"].append({"type": "web_search"})
                payload["include"].append("web_search_call.action.sources")
            if tool_choice is not None:
                payload["tool_choice"] = tool_choice
            endpoint = "responses"
        else:
            payload = {
                "model": self.model,
                "reasoning_effort": self.reasoning_effort,
                "messages": [{"role": "system", "content": session.system}, *history],
                "max_tokens": int(self.config.get("max_output_tokens", 4000)),
                "tools": [{"type": "function", "function": {**tool, "strict": False}} for tool in tools],
            }
            # Providers differ here. Explicit configuration replaces guesswork.
            if self.config.get("chat_use_max_completion_tokens", False):
                payload["max_completion_tokens"] = payload.pop("max_tokens")
            endpoint = "chat/completions"
        try:
            data = await self._post(endpoint, payload, session)
        except ModelBudgetExceeded:
            # A local limit stops before HTTP and says nothing about endpoint capability.
            raise
        except ReasoningEffortUnsupported:
            # This rejection says nothing about web_search support.
            raise
        except ModelError:
            if self.config.get("model_native_search", True) and session.native_search and self.protocol == "responses":
                observation = session.requests[-1] if session.requests else {}
                code = observation.get("status_code")
                state = "authentication_failed" if code in {401, 403} else (
                    "unsupported" if observation.get("native_tool_rejected") else "request_failed")
                self._native_observation(state, error=observation.get("error"), status_code=code)
            raise
        if self.protocol == "responses":
            if data.get("status") in {"failed", "incomplete", "cancelled", "in_progress", "queued"}:
                error = ModelProtocolError(f"模型响应未完成（{data.get('status')}）；请检查输出预算或服务状态")
                # Completed search observations remain useful even when final
                # generation is truncated. Do not expose or publish partial text,
                # execute partial function calls, or replay incomplete history.
                output = data.get("output") or []
                if isinstance(output, list) and all(isinstance(item, dict) for item in output):
                    events, sources = self._native_outputs(output)
                    if events or sources:
                        state = self._native_observation("observed", event_count=len(events), source_count=len(sources),
                            response_incomplete=True) if any(e.get("status") == "completed" for e in events) else self.native_status()
                        error.native_turn = ModelTurn(native_events=events, sources=sources, native_status=state)
                raise error
            output = data.get("output")
            if not isinstance(output, list) or not all(isinstance(item, dict) for item in output):
                raise ModelProtocolError("Responses 返回缺少 output 数组；端点可能不支持该协议")
            calls = [self._parse_call(item.get("call_id"), item.get("name"), item.get("arguments"))
                     for item in output if item.get("type") == "function_call"]
            parts = []
            annotations = []
            intermediate_message = False
            for item in output:
                if item.get("type") != "message":
                    continue
                content = item.get("content", [])
                if not isinstance(content, list) or not all(isinstance(part, dict) for part in content):
                    raise ModelProtocolError("Responses 消息内容格式异常")
                text_parts = [part.get("text", "") for part in content if part.get("type") == "output_text"]
                if not all(isinstance(part, str) for part in text_parts):
                    raise ModelProtocolError("Responses 返回的文本类型异常")
                # A completed HTTP response can contain an intermediate assistant
                # message. Explicit protocol metadata, never wording heuristics,
                # decides whether its text is eligible to finish the agent loop.
                # Older/compatible endpoints omitting phase keep their behavior.
                if item.get("phase") == "commentary" or item.get("status") in {"in_progress", "incomplete"}:
                    intermediate_message = True
                else:
                    parts.extend(text_parts)
                for part in content:
                    for annotation in part.get("annotations", []) if isinstance(part.get("annotations"), list) else []:
                        if isinstance(annotation, dict) and annotation.get("type") == "url_citation":
                            safe = self._source(annotation)
                            if safe:
                                annotations.append({**safe, "type": "url_citation", "message_id": item.get("id"),
                                    **{k: annotation[k] for k in ("start_index", "end_index") if isinstance(annotation.get(k), int)}})
            if not all(isinstance(part, str) for part in parts):
                raise ModelProtocolError("Responses 返回的文本类型异常")
            events, sources = self._native_outputs(output)
            sources_by_url = {source["url"]: source for source in sources}
            for annotation in annotations:
                sources_by_url.setdefault(annotation["url"], {k: annotation[k] for k in ("url", "title") if k in annotation})
            reported_usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
            usage = data.get("server_side_tool_usage") or reported_usage.get("server_side_tool_usage") or reported_usage.get("server_side_tool_usage_details") or {}
            observed = any(event.get("status") == "completed" for event in events) or (
                isinstance(usage, dict) and any("WEB_SEARCH" in k.upper() and isinstance(v, (int, float)) and v > 0 for k, v in usage.items()))
            state = self._native_observation("observed", event_count=len(events), source_count=len(sources_by_url),
                note="服务返回了原生搜索完成事件或非零工具用量；来源范围以实际返回内容为准。") if observed else self.native_status()
            turn = ModelTurn("\n".join(parts), calls, events, list(sources_by_url.values()), annotations,
                             bool((events or intermediate_message) and not calls and not "\n".join(parts).strip()), state)
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
        if not turn.calls and not turn.text.strip() and not turn.continuation_required:
            raise ModelProtocolError("模型返回空结果，未生成完整文本或本地函数调用")
        session.pending_calls = list(turn.calls)
        return turn

    @staticmethod
    def _source(value: Any) -> dict | None:
        from .native_sources import normalize_native_source
        return normalize_native_source(value)

    @classmethod
    def _native_outputs(cls, output: list[dict]) -> tuple[list[dict], list[dict]]:
        events, sources = [], []
        for item in output:
            if item.get("type") != "web_search_call":
                continue
            event = {k: item[k] for k in ("type", "id", "status") if isinstance(item.get(k), str)}
            action = item.get("action")
            if isinstance(action, dict):
                event["action"] = {k: copy.deepcopy(action[k]) for k in ("type", "query", "queries", "url", "pattern")
                                   if isinstance(action.get(k), (str, list))}
                for raw in action.get("sources", []) if isinstance(action.get("sources"), list) else []:
                    source = cls._source(raw)
                    if source:
                        sources.append(source)
                event["source_count"] = len(action.get("sources", [])) if isinstance(action.get("sources"), list) else 0
            events.append(event)
        return events, sources

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

    def _safe_provider_identifier(self, value):
        if not isinstance(value, str):
            return None
        return (value.replace(self.key, '[REDACTED]') if self.key else value)[:512]

    def _reflects_credential(self, value):
        if not self.key:
            return False
        if isinstance(value, str):
            return value == self.key or (len(self.key) >= 8 and self.key in value)
        if isinstance(value, list):
            return any(self._reflects_credential(item) for item in value)
        if isinstance(value, dict):
            return any(self._reflects_credential(key) or self._reflects_credential(item) for key, item in value.items())
        return False

    async def _post(self, endpoint: str, payload: dict, session: ModelSession | None = None) -> dict:
        retries = max(0, min(int(self.config.get("model_retries", 1)), 3))
        for attempt in range(retries + 1):
            if self.before_request:
                await self.before_request()
            started = time.perf_counter()
            observation = {"attempt": attempt + 1, "endpoint": endpoint, "model": self.model,
                           "base_url": self.base_url, "protocol": self.protocol,
                           "task_id": session.task_id if session else None,
                           "reasoning_effort": self.reasoning_effort,
                           "reasoning_effort_path": "reasoning.effort" if self.protocol == "responses" else "reasoning_effort",
                           "reasoning_effort_sent": (payload.get("reasoning") or {}).get("effort") if self.protocol == "responses" else payload.get("reasoning_effort"),
                           "request_accepted": None, "reported_reasoning_effort": None,
                           "agent_id": session.agent_id if session else "probe",
                           "stage": session.phase if session else "probe",
                           "input_chars": len(encode(payload)), "input_items": len(payload.get("input", payload.get("messages", []))),
                           "requested_max_output_tokens": payload.get("max_output_tokens", payload.get("max_completion_tokens", payload.get("max_tokens"))),
                           "output_bytes": 0, "request_id": None,
                           "native_search_requested": any(t.get("type") == "web_search" for t in payload.get("tools", [])),
                           "status_code": None, "usage": None, "error": None}
            observation.update({key: self.config[key] for key in ("provider_id", "provider_revision", "credential_revision", "role_id", "role_version", "prompt_version", "tool_version", "team_id", "account_id") if key in self.config})
            self.requests.append(observation)
            if session is not None:
                session.requests.append(observation)
            try:
                timeout = float(self.config.get("synthesis_request_timeout_seconds", self.config.get("model_request_timeout_seconds", 90))) if session and session.phase == "synthesis" else float(self.config.get("model_request_timeout_seconds", 90))
                observation["request_timeout_seconds"] = timeout
                # The HTTP total request and outer research-round deadlines are
                # separate. Do not restart a whole round for a request failure.
                async with asyncio.timeout(timeout):
                    response = await self.http.post(endpoint, json=payload, timeout=timeout)
            except TimeoutError as exc:
                observation.update(duration_ms=round((time.perf_counter() - started) * 1000, 2), error="RequestDeadlineExceeded")
                if attempt < retries:
                    await asyncio.sleep(min(2 ** attempt, 4))
                    continue
                raise ModelError("模型单次请求超过时限；已完成研究可从汇总检查点恢复") from exc
            except httpx.RequestError as exc:
                observation.update(duration_ms=round((time.perf_counter() - started) * 1000, 2), error=type(exc).__name__)
                if attempt < retries:
                    await asyncio.sleep(min(2 ** attempt, 4))
                    continue
                raise ModelError(f"模型网络请求失败（{type(exc).__name__}）；账户保存不受影响") from exc
            except asyncio.CancelledError:
                observation.update(duration_ms=round((time.perf_counter() - started) * 1000, 2), error="cancelled")
                raise
            observation.update(duration_ms=round((time.perf_counter() - started) * 1000, 2), status_code=response.status_code,
                               request_accepted=200 <= response.status_code < 300,
                               output_bytes=len(response.content),
                               request_id=self._safe_provider_identifier(response.headers.get("x-request-id") or response.headers.get("request-id")))
            if response.status_code >= 400:
                observation["error"] = f"HTTP {response.status_code}"
                if response.status_code in {400, 404, 422} or (response.status_code >= 500 and any(
                    code in response.text.lower() for code in ('unsupported_parameter', 'unsupported_value', 'invalid_parameter', 'invalid_value'))):
                    # Read only for classification; never log server bodies or
                    # trust them as proof that another effort would work.
                    message = response.text.lower()
                    effort_rejected = any(field in message for field in ("reasoning_effort", "reasoning.effort", "reasoning effort"))
                    # Some compatible endpoints identify only the rejected
                    # Responses object in their plain-text error, without param.
                    if re.search(r"\breasoning\b", message) and any(term in message for term in (
                        'unrecognized', 'unsupported', 'not supported', 'unknown parameter', 'unknown field', 'not allowed')):
                        effort_rejected = True
                    try:
                        error_body = response.json().get("error", {})
                    except (ValueError, AttributeError):
                        error_body = {}
                    if isinstance(error_body, dict):
                        effort_rejected = effort_rejected or error_body.get("param") in {"reasoning", "reasoning.effort", "reasoning_effort"}
                    if effort_rejected:
                        observation.update(reasoning_effort_rejected=True, error="reasoning_effort_rejected")
                        raise ReasoningEffortUnsupported(
                            f"模型端点 {self.base_url}（{self.protocol}）拒绝 {observation['reasoning_effort_path']}={self.reasoning_effort} "
                            f"（HTTP {response.status_code}）；请在设置中选择此端点支持的档位或更换端点。没有降档、删参或重试。")
                if response.status_code in {400, 404, 422} and observation["native_search_requested"]:
                    # Only use the body for classification; never emit it.
                    message = response.text.lower()
                    observation["native_tool_rejected"] = "web_search" in message and any(
                        word in message for word in ("unsupported", "not support", "not allowed", "invalid", "unknown"))
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
            if self._reflects_credential(data):
                observation['error'] = 'credential_reflected_by_provider'
                raise ModelProtocolError('供应商响应包含凭据反射，已停止处理；未写入报告、日志或模型后续上下文')
            root_field = "output" if self.protocol == "responses" else "choices"
            if root_field not in data and ("success" in data or "data" in data):
                # Support only the observed, explicit one-layer success/data
                # envelope. A standard root always wins, including a malformed
                # one: never replace it with an attractive nested completion.
                inner = data.get("data")
                shape = inner.get(root_field) if isinstance(inner, dict) else None
                if (data.get("success") is not True or not isinstance(inner, dict)
                        or not isinstance(shape, list) or not all(isinstance(item, dict) for item in shape)
                        or data.get("error") or inner.get("error") or inner.get("success") is False):
                    observation["error"] = "invalid_response_envelope"
                    raise ModelProtocolError("模型服务的 success/data 封装未明确成功或缺少当前协议结果；未使用不完整或多层封装")
                observation["response_envelope"] = "data"
                if isinstance(data.get("usage"), dict):
                    # Forwarder counters can disagree with the model's own
                    # counters. Keep them separate; never add or substitute them.
                    observation["forwarder_usage"] = _numeric_counters(data["usage"])
                data = inner
            if isinstance(data.get("model"), str):
                observation["reported_model"] = self._safe_provider_identifier(data["model"])
            reported_effort = (data.get("reasoning") or {}).get("effort") if isinstance(data.get("reasoning"), dict) else data.get("reasoning_effort")
            if reported_effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max"):
                observation["reported_reasoning_effort"] = reported_effort
            if isinstance(data.get("usage"), dict):
                # Preserve only numeric usage counters, never raw provider fields.
                observation["usage"] = _numeric_counters(data["usage"])
                reported = observation["usage"].get("output_tokens", observation["usage"].get("completion_tokens"))
                requested = observation["requested_max_output_tokens"]
                if isinstance(reported, (int, float)) and isinstance(requested, (int, float)):
                    # This compares provider-reported counters with the request;
                    # it does not independently measure tokens, cost or capability.
                    observation["reported_output_exceeds_requested"] = reported > requested
            if isinstance(data.get("server_side_tool_usage"), dict):
                observation["server_side_tool_usage"] = _numeric_counters(data["server_side_tool_usage"])
            if isinstance(data.get("id"), str):
                observation["response_id"] = self._safe_provider_identifier(data["id"])
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
    session.native_search = False
    session.agent_id, session.phase = "capability-probe", "function_probe"
    try:
        first = await client.complete(session, [tool])
        if len(first.calls) != 1 or first.calls[0].name != "capability_echo" or first.calls[0].error:
            raise ModelProtocolError("能力检查失败：模型没有按要求调用函数")
        client.add_results(session, [(first.calls[0], {"token": nonce})])
        second = await client.complete(session, [tool])
        if second.calls or nonce not in second.text:
            raise ModelProtocolError("能力检查失败：模型未正确续接函数返回值")
        return {"ok": True, "protocol": client.protocol, "model": client.model,
                "fingerprint": native_search_fingerprint(config), "model_config": model_config_summary(config),
                "checks": ["function_call", "function_result_continuation"],
                "model_usage": client.usage_summary(client.requests),
                "requests": copy.deepcopy(client.requests),
                "note": "已实测此端点和模型的两轮工具协议；不等于研究质量验收。"}
    except ModelError as exc:
        # Keep the existing exception contract for CLI callers while exposing
        # paid failed-attempt diagnostics to the settings API.
        exc.probe_diagnostics = {"ok": False, "error": str(exc),
            "status": "configuration_error" if isinstance(exc, ModelConfigurationError) else "request_failed",
            "fingerprint": native_search_fingerprint(config), "model_config": model_config_summary(config),
            "requests": copy.deepcopy(client.requests), "model_usage": client.usage_summary(client.requests)}
        raise
    finally:
        await client.aclose()


async def probe_native_search(config: dict[str, Any], *, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    """One bounded, explicitly requested paid native-tool check; no silent fallback.

    The caller persists the fingerprinted result and never probes on page refresh.
    A search call may involve multiple provider-side operations; usage is returned
    as observed, never guessed from the application's request count.
    """
    status = native_search_status(config)
    if status["status"] in {"disabled", "unsupported"}:
        return {**status, "ok": False, "requests": []}
    cfg = {**config, "model_retries": 0,
           "model_request_timeout_seconds": min(float(config.get("model_request_timeout_seconds", 90)), 60),
           "max_output_tokens": min(int(config.get("max_output_tokens", 4000)), 1200)}
    try:
        client = ModelClient(cfg, transport=transport)
    except ModelConfigurationError as exc:
        return {"ok": False, "status": "authentication_failed" if "缺少环境变量" in str(exc) else "configuration_error",
                "fingerprint": native_search_fingerprint(config), "error": str(exc), "requests": []}
    session = client.session("Use the native web_search tool once to discover an official Kweichow Moutai investor financial report. Return one discovered official URL and its title. Do not use memory or claim local download.",
                             "Bounded native search capability check; only the company is provided, no source URL.")
    session.agent_id, session.phase = "capability-probe", "native_search_probe"
    try:
        turn = await client.complete(session, [], tool_choice="required")
        state = client.native_status()
        # A new explicit probe with no execution evidence cannot inherit an old
        # 'observed' result and misrepresent this request as having searched.
        actual = bool(turn.native_events and any(e.get("status") == "completed" for e in turn.native_events))
        if not actual and state.get("checked_at") == status.get("checked_at"):
            state = client._native_observation("unverified", note="请求完成但服务未返回可观察的搜索执行；可能忽略了工具参数。")
        return {**state, "ok": state["status"] == "observed", "protocol": client.protocol,
                "model": client.model, "native_events": turn.native_events, "sources": turn.sources,
                "annotations": turn.annotations, "text": turn.text,
                "requests": copy.deepcopy(client.requests), "model_usage": client.usage_summary(client.requests)}
    except ReasoningEffortUnsupported as exc:
        return {"status": "configuration_error", "fingerprint": native_search_fingerprint(config),
                "ok": False, "error": str(exc), "native_search_status": client.native_status()["status"],
                "requests": copy.deepcopy(client.requests), "model_usage": client.usage_summary(client.requests)}
    except ModelError as exc:
        state = client.native_status()
        if state["status"] not in {"unsupported", "authentication_failed", "request_failed"}:
            state = client._native_observation("request_failed", error=type(exc).__name__)
        return {**state, "ok": False, "error": str(exc).replace(client.key, "[REDACTED]") if client.key else str(exc),
                "requests": copy.deepcopy(client.requests), "model_usage": client.usage_summary(client.requests)}
    finally:
        await client.aclose()
