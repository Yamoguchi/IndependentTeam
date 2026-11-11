"""Structured run events and shared request reservations."""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from hashlib import sha256
from itertools import count
from time import monotonic
from typing import TYPE_CHECKING, Any, TextIO
from uuid import uuid4

from contest_harness.json_codec import canonical_json
from contest_harness.settings import MIN_ADAPTIVE_OUTPUT
from contest_harness.telemetry.ledger import BudgetLedger
from contest_harness.telemetry.usage import BudgetExceeded, CallEstimate, ProxyUsage, Usage

if TYPE_CHECKING:
    from openjiuwen.core.foundation.llm import AssistantMessage


def describe(value: Any) -> dict[str, int | str]:
    text = value if isinstance(value, str) else canonical_json(value)
    data = text.encode("utf-8", errors="surrogatepass")
    return {
        "chars": len(text),
        "utf8_bytes": len(data),
        "sha256": sha256(data).hexdigest(),
    }


def error_fields(error: BaseException) -> dict[str, Any]:
    cause = error
    while cause.__cause__ is not None:
        cause = cause.__cause__
    fields: dict[str, Any] = {"error_type": type(cause).__name__}
    status = getattr(cause, "status_code", None)
    if isinstance(status, int):
        fields["http_status"] = status
    return fields


@dataclass
class TaskTrace:
    index: int
    task_id: str
    session_id: int = 1
    wait_for_budget: bool = False
    published: bool = False
    usage: Usage = field(default_factory=Usage)
    last_call_id: int | None = None
    last_input_tokens: int | None = None
    last_request: dict[str, Any] | None = None


@dataclass
class ModelCall:
    id: int
    model: str
    started_at: float
    tool_choice: Any
    response: AssistantMessage | None = None
    estimate: CallEstimate | None = None
    proxy_usage: ProxyUsage | None = None
    not_sent: bool = False
    denied: bool = False
    timed_out: bool = False
    wire_finish_reason: str | None = None
    generation_profile: str | None = None
    generation_recovery: bool = False
    generation_rejection: str | None = None
    budget_wait_ms: float = 0


class RunTrace:
    def __init__(
        self, stream: TextIO | None, *, redact: tuple[str, ...] = (), limit_tenths: int | None = None
    ):
        self.stream = stream
        self.run_id = uuid4().hex
        self.started_at = monotonic()
        self.ledger = BudgetLedger(limit_tenths)
        self._secrets = tuple(sorted(filter(None, redact), key=len, reverse=True))
        self._sequence = count(1)
        self._call_ids = count(1)
        self._task = ContextVar[TaskTrace | None]("trace_task", default=None)
        self._call = ContextVar[ModelCall | None]("trace_model_call", default=None)

    @property
    def enabled(self) -> bool:
        return self.stream is not None

    def _redact(self, value: Any) -> Any:
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, "[redacted]")
        elif isinstance(value, dict):
            return {key: self._redact(item) for key, item in value.items()}
        elif isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def emit(self, event: str, **fields: Any) -> None:
        if self.stream is None:
            return
        entry = {
            "schema_version": 1,
            "run_id": self.run_id,
            "sequence": next(self._sequence),
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "elapsed_ms": round((monotonic() - self.started_at) * 1000, 3),
            "event": event,
        }
        task = self._task.get()
        if task is not None:
            entry.update(task_index=task.index, task_id=task.task_id, session_id=task.session_id)
        try:
            # Encode any isolated Unicode surrogate so trace output stays valid JSON.
            print(
                json.dumps(self._redact({**entry, **fields}), ensure_ascii=True),
                file=self.stream,
                flush=True,
            )
        except OSError:
            self.stream = None
            try:
                print(
                    "Detailed trace disabled after a write error.",
                    file=sys.stderr,
                    flush=True,
                )
            except OSError:
                pass

    @contextmanager
    def task(
        self, index: int, directory: str, *, task_id=None, session_id=1, wait_for_budget=False, resume=None,
    ) -> Iterator[TaskTrace]:
        task = resume or TaskTrace(index, task_id or directory, wait_for_budget=wait_for_budget)
        task.session_id = session_id
        token = self._task.set(task)
        try:
            self.emit("task.resumed" if resume else "task.started", directory=directory)
            yield task
        finally:
            self._task.reset(token)

    @contextmanager
    def model_call(self, model: str, tool_choice: Any) -> Iterator[ModelCall]:
        task = self._task.get()
        call = ModelCall(next(self._call_ids), model, monotonic(), tool_choice)
        token = self._call.set(call)
        if task is not None:
            task.last_call_id = call.id
        error = None
        try:
            yield call
        except BaseException as exc:
            error = exc
            raise
        finally:
            self._call.reset(token)
            self._finish_call(call, task, error)

    @property
    def usage(self) -> Usage:
        return self.ledger.usage

    @property
    def limit_tenths(self) -> int | None:
        return self.ledger.limit_tenths

    @property
    def in_flight_tenths(self) -> int:
        return self.ledger.in_flight_tenths

    def budget_report(self) -> dict[str, int | None]:
        return self.ledger.report()

    def admit(self, model: str, estimate: CallEstimate) -> ModelCall:
        """Check and reserve without yielding, shared by all sessions on the run's event loop."""
        call = self._call.get()
        if call is None or call.estimate is not None or call.denied:
            raise RuntimeError("Expected one transport attempt per traced model invocation")
        call.model = model
        generation = {
            "generation_profile": call.generation_profile,
            "generation_recovery": call.generation_recovery,
        }
        if not self.ledger.reserve(estimate):
            call.denied = True
            self.emit(
                "llm.denied", call_id=call.id, model=model, estimate=estimate.report(),
                reason="proxy_usage_exceeded_reservation" if self.ledger.reservation_breached else "insufficient_budget",
                **generation, **self.budget_report(),
            )
            raise BudgetExceeded("Insufficient local budget for the next request")
        call.estimate = estimate
        # A recovered start without a finish may have reached the proxy.
        self.emit(
            "llm.started", call_id=call.id, model=model, estimate=estimate.report(),
            **generation, **self.budget_report(),
        )
        return call

    async def reserve(self, model: str, estimate: CallEstimate) -> ModelCall:
        task = self._task.get()
        call = self._call.get()
        wait = task is not None and task.wait_for_budget
        if self.limit_tenths is not None and not self.ledger.reservation_breached:
            started = None
            try:
                while True:
                    candidate = estimate
                    remaining = self.ledger.remaining_tenths
                    if call.generation_profile and not call.generation_recovery:
                        possible = remaining + (self.in_flight_tenths if wait else 0)
                        available_output = (possible - estimate.input_tokens) // 10 - estimate.reasoning_margin
                        candidate = replace(estimate, output_limit=min(
                            estimate.output_limit, max(MIN_ADAPTIVE_OUTPUT, available_output),
                        ))
                    if self.ledger.reservation_breached or not wait or not self.ledger.may_fit_after_settlement(candidate):
                        break
                    if started is None:
                        started = monotonic()
                        self.emit("llm.waiting_for_budget", call_id=call.id, model=model,
                                  estimate=candidate.report(), **self.budget_report())
                    await self.ledger.wait_for_settlement()
                estimate = candidate
            finally:
                if started is not None:
                    call.budget_wait_ms = round((monotonic() - started) * 1000, 3)
        # No await between the final budget check and acquiring the reservation.
        return self.admit(model, estimate)

    def request(
        self,
        *,
        model_name: str,
        model_provider: str,
        messages: list[dict] | None,
        tools: list[dict] | None,
        **options: Any,
    ) -> None:
        """Observe the final messages and tool schemas at the proxy adapter."""
        call = self._call.get()
        if not self.enabled or call is None:
            return
        call.model = model_name
        message_rows = []
        roles: dict[str, dict[str, int]] = {}
        for message in messages or []:
            role = message["role"]
            wire = describe(canonical_json(message))
            content = describe(message.get("content") or "")
            message_rows.append(
                {
                    "role": role,
                    "message": wire,
                    "content": content,
                    "tool_call_id": message.get("tool_call_id"),
                    "tool_calls": [
                        {
                            "id": item.get("id"),
                            "name": (item.get("function") or {}).get("name"),
                            "arguments": describe((item.get("function") or {}).get("arguments") or ""),
                        }
                        for item in message.get("tool_calls") or []
                    ],
                    "reasoning": describe(message.get("reasoning_content") or ""),
                }
            )
            bucket = roles.setdefault(role, {"messages": 0, "serialized_bytes": 0})
            bucket["messages"] += 1
            bucket["serialized_bytes"] += int(wire["utf8_bytes"])

        message_shape = describe(canonical_json(messages or []))
        tool_shape = describe(canonical_json(tools or []))
        fingerprints = [row["message"]["sha256"] for row in message_rows]
        current = {
            "call_id": call.id,
            "fingerprints": fingerprints,
            "message_bytes": message_shape["utf8_bytes"],
            "tool_bytes": tool_shape["utf8_bytes"],
        }
        task = self._task.get()
        previous = task.last_request if task is not None else None
        delta = None
        if previous is not None:
            shared_prefix = 0
            for old, new in zip(previous["fingerprints"], fingerprints):
                if old != new:
                    break
                shared_prefix += 1
            delta = {
                "previous_call_id": previous["call_id"],
                "shared_prefix_messages": shared_prefix,
                "previous_tail_messages": len(previous["fingerprints"]) - shared_prefix,
                "current_tail_messages": len(fingerprints) - shared_prefix,
                "message_bytes_change": int(current["message_bytes"]) - previous["message_bytes"],
                "tool_schema_bytes_change": int(current["tool_bytes"]) - previous["tool_bytes"],
            }
        if task is not None:
            task.last_request = current
        self.emit(
            "llm.request",
            call_id=call.id,
            model=model_name,
            provider=model_provider,
            settings={
                **{
                    key: options.get(key)
                    for key in (
                        "temperature",
                        "top_p",
                        "max_tokens",
                        "frequency_penalty",
                        "presence_penalty",
                    )
                },
                "tool_choice": call.tool_choice,
                "stop": describe(options["stop"]) if options.get("stop") else None,
            },
            context={
                "messages": message_rows,
                "serialized": message_shape,
                "by_role": roles,
                "delta": delta,
            },
            tools={
                "serialized": tool_shape,
                "items": [
                    {
                        "name": (tool.get("function") or tool).get("name"),
                        "schema": describe(canonical_json(tool)),
                    }
                    for tool in tools or []
                ],
            },
        )

    def _finish_call(self, call: ModelCall, task: TaskTrace | None, error: BaseException | None) -> None:
        response = call.response
        proxy = call.proxy_usage
        estimate = call.estimate
        if estimate is None or call.not_sent:
            usage = Usage(denied_calls=int(call.denied), not_sent_calls=int(not call.denied))
            status = "budget_denied" if call.denied else "not_sent"
        else:
            usage = Usage.reconcile(estimate, proxy or ProxyUsage())
            status = (
                "cancelled"
                if isinstance(error, asyncio.CancelledError)
                else "timeout"
                if call.timed_out or isinstance(error, TimeoutError)
                else "error"
                if error is not None
                else "ok"
            )
        self.ledger.settle(estimate, usage)
        if task is not None:
            task.usage.add(usage)
        if not self.enabled:
            return

        input_delta = None
        input_tokens = proxy.input_tokens if proxy is not None else None
        if task is not None:
            previous = task.last_input_tokens
            if input_tokens is not None and previous is not None:
                input_delta = input_tokens - previous
            if usage.calls:
                task.last_input_tokens = input_tokens
        details = error_fields(error) if error is not None else {}
        self.emit(
            "llm.finished",
            call_id=call.id,
            model=call.model,
            status=status,
            duration_ms=round((monotonic() - call.started_at) * 1000, 3),
            budget_wait_ms=call.budget_wait_ms,
            usage=usage.report(),
            usage_status="unknown"
            if usage.calls > usage.measured_calls
            else "exact"
            if usage.calls
            else "not_sent",
            estimate=estimate.report() if estimate is not None else None,
            budget=self.budget_report(),
            proxy_usage=vars(proxy) if proxy is not None else None,
            input_tokens_change=input_delta,
            task_output_equivalent=task.usage.report()["output_equivalent"] if task is not None else None,
            finish_reason=response.finish_reason if response is not None else None,
            wire_finish_reason=call.wire_finish_reason,
            generation_profile=call.generation_profile,
            generation_recovery=call.generation_recovery,
            generation_rejection=call.generation_rejection,
            response={
                "content": describe(response.content or ""),
                "reasoning": describe(response.reasoning_content or ""),
                "tool_calls": [
                    {
                        "id": item.id,
                        "name": item.name,
                        "arguments": describe(item.arguments),
                    }
                    for item in response.tool_calls or []
                ],
            }
            if response is not None
            else None,
            **details,
        )

    def tool_event(self, event: str, **fields: Any) -> None:
        task = self._task.get()
        self.emit(event, call_id=task.last_call_id if task is not None else None, **fields)
