"""openJiuwen adapter for the OpenAI-compatible organizer proxy."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar

import httpx
from openai import AsyncOpenAI
from openjiuwen.core.foundation.llm import Model, ModelClientConfig, ModelRequestConfig, UserMessage
from openjiuwen.core.foundation.llm.model_clients.openai_model_client import OpenAIModelClient

from contest_harness.agent.generation import RECOVERY_HINT, GenerationFailed, GenerationPolicy
from contest_harness.agent.model_profiles import PROFILES, REQUEST_TIMEOUT
from contest_harness.agent.proxy_transport import BudgetTransport
from contest_harness.telemetry.trace import RunTrace


class TracedModel(Model):
    def __init__(
        self, trace: RunTrace, model_name: str, base_url: str, key: str, *, timeout: float = REQUEST_TIMEOUT
    ):
        super().__init__(
            ModelClientConfig(
                client_provider="OpenAI",
                api_base=base_url,
                api_key=key,
                timeout=timeout,
                max_retries=0,
                use_shared_llm_http_client=False,
            ),
            ModelRequestConfig.model_validate(
                {"model": model_name, "temperature": 0, "top_p": 1, "tool_choice": "required"}
            ),
        )
        self.trace = trace
        self._generation = ContextVar[GenerationPolicy | None]("generation_policy", default=None)
        self._allow_terminal_signal = ContextVar("allow_terminal_signal", default=False)
        # The pinned openJiuwen adapter has no public HTTP client injection.
        # Keep its message/response conversion and replace only its client factory.
        if not isinstance(self._client, OpenAIModelClient):
            raise RuntimeError("Expected the openJiuwen OpenAI adapter")
        self._client._build_async_openai_client = self._build_proxy_client

    def _build_proxy_client(self, timeout: float | None = None) -> AsyncOpenAI:
        return AsyncOpenAI(
            api_key=self.model_client_config.api_key,
            base_url=self.model_client_config.api_base,
            timeout=timeout or self.model_client_config.timeout,
            max_retries=0,
            http_client=httpx.AsyncClient(
                transport=BudgetTransport(self.trace), follow_redirects=False, trust_env=False
            ),
        )

    @contextmanager
    def generation(self, policy, *, allow_terminal_signal=False):
        token = self._generation.set(policy)
        signal_token = self._allow_terminal_signal.set(allow_terminal_signal)
        try:
            yield
        finally:
            self._allow_terminal_signal.reset(signal_token)
            self._generation.reset(token)

    async def invoke(self, messages, **kwargs):
        model = kwargs.get("model") or self.model_config.model_name
        if model not in PROFILES:
            raise ValueError("Unsupported proxy model")
        timeout = self.model_client_config.timeout
        policy = self._generation.get()
        if self._allow_terminal_signal.get():
            kwargs["tool_choice"] = "auto"
        profile = policy.profile() if policy else None
        if policy:
            kwargs["max_tokens"] = policy.limits[profile]
        elif kwargs.get("max_tokens") is None:
            kwargs["max_tokens"] = PROFILES[model][0]

        request_messages = messages
        for attempt in range(2):
            with self.trace.model_call(model, kwargs.get("tool_choice", self.model_config.tool_choice)) as call:
                call.generation_profile = profile
                call.generation_recovery = bool(attempt)
                async with asyncio.timeout(timeout):
                    call.response = await super().invoke(request_messages, **kwargs)
                if not policy:
                    if call.response.tool_calls and call.wire_finish_reason not in {"stop", "tool_calls"}:
                        call.generation_rejection = "incomplete_tool_completion"
                        raise GenerationFailed("Proxy returned an incomplete tool completion.")
                    return call.response
                reason = policy.rejection(call.response, call.wire_finish_reason)
                call.generation_rejection = reason
                if reason is None:
                    return call.response
                if reason == "invalid_arguments":
                    for proposed in call.response.tool_calls or ():
                        if proposed.name not in policy.schemas:
                            self.trace.tool_event(
                                "tool.unavailable", tool=proposed.name, tool_call_id=proposed.id,
                                reason="schema_not_offered",
                            )

            limit = policy.recovery_limit(reason, kwargs["max_tokens"]) if not attempt else None
            if limit is None:
                policy.failed = True
                raise GenerationFailed(f"Completion rejected: {reason}")
            policy.recoveries += 1
            profile = "recovery"
            kwargs["max_tokens"] = limit
            request_messages = [*messages, UserMessage(content=RECOVERY_HINT.format(reason=reason))]
