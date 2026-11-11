"""Token estimation and reconciliation with proxy usage."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


def budget_tenths(value: str) -> int:
    """Parse a nonnegative CLI limit without rounding a floating-point value."""
    if not re.fullmatch(r"[0-9]+(?:\.[0-9])?", value):
        raise ValueError("Expected a nonnegative token limit with at most one decimal place")
    whole, _, fraction = value.partition(".")
    return 10 * int(whole) + int(fraction or "0")


class BudgetExceeded(Exception):
    pass


@dataclass(frozen=True)
class CallEstimate:
    input_tokens: int
    output_limit: int
    reasoning_margin: int

    @property
    def output_tokens(self) -> int:
        return self.output_limit + self.reasoning_margin

    @property
    def tenths(self) -> int:
        return self.input_tokens + 10 * self.output_tokens

    def report(self) -> dict[str, int]:
        return {**vars(self), "output_tokens": self.output_tokens, "tenths": self.tenths}


def token_count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


@dataclass(frozen=True)
class ProxyUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    cached_input_tokens: int | None = None
    reasoning_tokens: int | None = None
    total_tokens: int | None = None
    billable_output_tokens: int | None = None
    reasoning_accounting: str = "unknown"

    @classmethod
    def read(cls, raw: Any) -> ProxyUsage:
        if not isinstance(raw, dict):
            return cls()
        prompt = token_count(raw.get("prompt_tokens"))
        completion = token_count(raw.get("completion_tokens"))
        total = token_count(raw.get("total_tokens"))
        prompt_details = raw.get("prompt_tokens_details")
        completion_details = raw.get("completion_tokens_details")
        hit = token_count(raw.get("prompt_cache_hit_tokens"))
        miss = token_count(raw.get("prompt_cache_miss_tokens"))
        cached = (
            token_count(prompt_details.get("cached_tokens")) if isinstance(prompt_details, dict) else None
        )
        if cached is None:
            cached = next(
                (
                    count
                    for key in (
                        "prompt_cache_hit_tokens",
                        "cached_input_tokens",
                        "cache_tokens",
                        "cached_tokens",
                        "prompt_cached_tokens",
                    )
                    if (count := token_count(raw.get(key))) is not None
                ),
                None,
            )
        if hit is not None and miss is not None:
            # An explicit hit/miss split accounts for the whole prompt.
            if prompt in (None, miss, hit + miss):
                prompt = hit + miss
            else:
                prompt = None
        if cached is not None and prompt is not None and cached > prompt:
            prompt = None
        nested = (
            token_count(completion_details.get("reasoning_tokens"))
            if isinstance(completion_details, dict)
            else None
        )
        flat = next(
            (
                count
                for key in ("reasoning_tokens", "reasoningTokens", "thinking_tokens", "thoughtsTokenCount")
                if (count := token_count(raw.get(key))) is not None
            ),
            None,
        )
        reasoning = nested if nested is not None else flat
        billable = None
        accounting = "unknown"
        if completion is not None and (nested is None or flat is None or nested == flat):
            if prompt is not None and total is not None:
                difference = total - prompt - completion
                if difference == 0 and (reasoning or 0) <= completion:
                    billable, accounting = completion, "included"
                elif reasoning and difference == reasoning:
                    billable, accounting = completion + reasoning, "separate"
            elif (nested is not None and nested <= completion) or flat in (None, 0):
                # The standard completion details field describes a subset.
                billable, accounting = completion, "included"
        return cls(prompt, completion, cached, reasoning, total, billable, accounting)


@dataclass
class Usage:
    calls: int = 0
    measured_calls: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    held_tenths: int = 0
    estimate_overruns: int = 0
    estimate_overrun_tenths: int = 0
    denied_calls: int = 0
    not_sent_calls: int = 0

    @classmethod
    def reconcile(cls, estimate: CallEstimate, proxy: ProxyUsage) -> Usage:
        input_tokens = (
            proxy.input_tokens if proxy.input_tokens is not None else proxy.cached_input_tokens or 0
        )
        output_tokens = proxy.billable_output_tokens
        exact = proxy.input_tokens is not None and output_tokens is not None
        if output_tokens is None:
            # Either counter is a lower bound when their overlap is ambiguous.
            output_tokens = max(proxy.output_tokens or 0, proxy.reasoning_tokens or 0)
        held = 0
        if proxy.input_tokens is None:
            held += max(0, estimate.input_tokens - input_tokens)
        if proxy.billable_output_tokens is None:
            held += 10 * max(0, estimate.output_tokens - output_tokens)
        overrun = max(0, input_tokens - estimate.input_tokens) + 10 * max(
            0, output_tokens - estimate.output_tokens
        )
        return cls(
            calls=1,
            measured_calls=int(exact),
            input_tokens=input_tokens,
            cached_input_tokens=proxy.cached_input_tokens or 0,
            output_tokens=output_tokens,
            reasoning_tokens=proxy.reasoning_tokens or 0,
            held_tenths=held,
            estimate_overruns=int(overrun > 0),
            estimate_overrun_tenths=overrun,
        )

    @property
    def measured_tenths(self) -> int:
        return self.input_tokens + 10 * self.output_tokens

    def add(self, usage: Usage) -> None:
        for name, value in vars(usage).items():
            setattr(self, name, getattr(self, name) + value)

    def report(self) -> dict[str, Any]:
        exact = self.calls == self.measured_calls
        return {
            **vars(self),
            "unknown_usage_calls": self.calls - self.measured_calls,
            "usage_status": "exact" if exact else "unknown",
            "measured_tenths": self.measured_tenths,
            "exact_tenths": self.measured_tenths if exact else None,
            "measured_output_equivalent": self.measured_tenths / 10,
            "output_equivalent": self.measured_tenths / 10 if exact else None,
            "usage_complete": exact,
        }
