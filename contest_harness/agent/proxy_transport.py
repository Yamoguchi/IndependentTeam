"""Reserve and reconcile token usage at the HTTP boundary."""

import json

import httpx

from contest_harness.agent.model_profiles import PROFILES
from contest_harness.telemetry.trace import RunTrace
from contest_harness.telemetry.usage import CallEstimate, ProxyUsage, token_count


class BudgetTransport(httpx.AsyncHTTPTransport):
    def __init__(self, trace: RunTrace):
        super().__init__(retries=0, trust_env=False)
        self.trace = trace

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        model = body.get("model")
        if model not in PROFILES or body.get("stream") or body.get("n", 1) != 1:
            raise ValueError("Expected an approved model with one non-streaming completion")
        cap, reasoning_margin, template_margin = PROFILES[model]
        output_limit = token_count(body.get("max_tokens"))
        if not output_limit or output_limit > cap or "max_completion_tokens" in body:
            raise ValueError("Expected a single bounded generation limit on the wire")
        messages = body["messages"]
        tools = body.get("tools") or []
        # A byte-level tokenizer can emit one token per byte. Allow for the
        # proxy's chat template in addition to the entire serialized request.
        input_bound = len(request.content) + template_margin + 32 * (len(messages) + len(tools))
        call = await self.trace.reserve(model, CallEstimate(input_bound, output_limit, reasoning_margin))
        try:
            if call.estimate.output_limit != output_limit:
                # Decreasing the numeric cap can only shorten the original JSON body.
                body["max_tokens"] = call.estimate.output_limit
                headers = request.headers.copy()
                del headers["content-length"]
                payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                if len(payload) > len(request.content):
                    raise ValueError("Rewriting the generation limit increased the reserved request size.")
                request = httpx.Request(
                    request.method, request.url, headers=headers, extensions=request.extensions,
                    content=payload,
                )
            self.trace.request(
                model_name=model,
                model_provider="OpenAI",
                messages=messages,
                tools=tools,
                **{
                    key: body.get(key)
                    for key in (
                        "max_tokens",
                        "temperature",
                        "top_p",
                        "frequency_penalty",
                        "presence_penalty",
                        "stop",
                    )
                },
            )
        except BaseException:
            call.not_sent = True
            raise
        try:
            response = await super().handle_async_request(request)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            # No HTTP request bytes were written, including on a TLS handshake failure.
            call.not_sent = True
            raise
        except httpx.TimeoutException:
            call.timed_out = True
            raise
        try:
            await response.aread()
        except BaseException as error:
            call.timed_out = isinstance(error, httpx.TimeoutException)
            await response.aclose()
            raise
        try:
            payload = response.json()
        except ValueError:
            return response
        if isinstance(payload, dict):
            if "usage" in payload:
                call.proxy_usage = ProxyUsage.read(payload["usage"])
            choices = payload.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                reason = choices[0].get("finish_reason")
                if isinstance(reason, str):
                    call.wire_finish_reason = reason
        return response
