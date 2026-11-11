"""Pre-run task forecasts from independent full attempts or conservative defaults."""

import json
from collections import defaultdict
from math import ceil, isfinite
from statistics import median

from contest_harness.settings import MIN_COST_SAMPLES, MIN_SUCCESS_SAMPLES
from contest_harness.telemetry.usage import CallEstimate

SCHEMA_VERSION = 1
FULL_ATTEMPTS = {"submitted", "no_answer", "generation_failed", "step_limit"}
# Ordinary ReAct follow-ups after the first completion, excluding test review.
REPEATS = {"math": 2, "search": 3, "data_analysis": 3, "coding": 5}


def settings_for(mode, generation_limits, direct_publish, step_limit, escalation_penalty):
    return {
        "mode": mode, "generation_limits": generation_limits, "direct_publish": direct_publish,
        "step_limit": step_limit, "escalation_penalty": escalation_penalty,
    }


class PlanningHistory:
    def __init__(self, rows=()):
        self.groups = defaultdict(list)
        for row in rows:
            self.groups[row["task_type"], row["route"], row["model"]].append(row)

    @classmethod
    def load(cls, path):
        if path is None:
            return cls()
        rows = []
        with path.open(encoding="utf-8") as source:
            for line in source:
                row = json.loads(line)
                if (
                    row["schema_version"] != SCHEMA_VERSION
                    or type(row["exact_tenths"]) is not int or row["exact_tenths"] < 0
                    or type(row["passed"]) is not bool
                    or type(row["local_ms"]) not in {int, float}
                    or not isfinite(row["local_ms"]) or row["local_ms"] < 0
                    or any(not isinstance(row[key], str) or not row[key] for key in (
                        "task_id", "task_type", "route", "instruction_sha256", "source_run_id",
                    ))
                    or row["proxy_kind"] not in {"organizer", "test-double"}
                    or not isinstance(row["settings"], dict)
                    or "smart_model" not in row
                ):
                    raise ValueError("Invalid planning history observation.")
                rows.append(row)
        return cls(rows)

    def independent_of(self, task_ids, instruction_hashes):
        return PlanningHistory(
            row for group in self.groups.values() for row in group
            if row["task_id"] not in task_ids and row["instruction_sha256"] not in instruction_hashes
        )

    def observations(self, kind, route, settings):
        # Repeated runs on one instruction supply one observation. Prefer real
        # usage whenever both real and scripted observations are available.
        rows = self.groups.get((kind, route.name, route.model), ())
        matches = [
            row for row in rows
            if row["settings"] == settings
            and row["smart_model"] == (route.smart.model if route.name == "CASCADE" else None)
        ]
        organizer = [row for row in matches if row["proxy_kind"] == "organizer"]
        distinct = {}
        for row in organizer or matches:
            distinct.setdefault(row["instruction_sha256"], row)
        return list(distinct.values())


def estimate_task(kind, route, settings, history, *, first_reserve=0, input_bytes=0, reasoning=False):
    """Forecast whole-task spend, with a separate first-request admission floor."""
    local = route.name == "LOCAL"
    rows = history.observations(kind, route, settings)
    positive = [row["exact_tenths"] for row in rows if row["exact_tenths"] > 0]
    enough_costs = len(positive) >= MIN_COST_SAMPLES
    repeats = min(REPEATS.get(kind, 3), settings["step_limit"] - 1) if not local else 0
    # Byte counts are available before tokenization. Include tool schemas and
    # incremental context separately from the allowance for generated output.
    output = 600 if reasoning else 150
    ordinary = ceil(input_bytes / 3) + 10 * output
    components = {
        "initial_request": 0 if local else ordinary,
        "repeat_requests": ordinary * repeats,
        "context_restoration": (repeats + 1) * 240 if repeats else 0,
        "generation_recovery": ordinary if settings["mode"] == "B+C+D" and not local else 0,
        "llm_verification": ordinary if kind == "coding" and repeats else 0,
        # Include a full alternate attempt as a conservative sparse-group prior.
        "escalation": (
            max(ordinary * 3, ceil(route.smart.mean_cost * 10)) + 1800
            if route.name == "CASCADE" else 0
        ),
    }
    # A local route has zero token spend. Only ranking uses a denominator floor.
    source = "local" if local else "group_median" if enough_costs else "conservative_fallback"
    cost = 0 if local else ceil(median(positive)) if enough_costs else max(
        sum(components.values()), max(positive, default=0)
    )
    success = [row["passed"] for row in rows if row["proxy_kind"] == "organizer"]
    probability = (sum(success) + 1) / (len(success) + 2) if len(success) >= MIN_SUCCESS_SAMPLES else None
    local_ms = round(median(row["local_ms"] for row in rows), 3) if len(rows) >= MIN_COST_SAMPLES else (
        20 + route.features["files_seen"] * 2 + (1000 if kind == "coding" else 0)
    )
    return {
        "estimated_tenths": cost,
        "priority_denominator_tenths": max(1, cost),
        "admission_tenths": 0 if local else max(cost, first_reserve),
        "first_reserve_tenths": first_reserve,
        "estimated_local_ms": local_ms,
        "cost_source": source,
        "cost_samples": len(rows) if local else len(positive),
        "success_probability": probability,
        "success_samples": len(success),
        "history_runs": sorted({row["source_run_id"] for row in rows}),
        "fallback_components_tenths": components if source == "conservative_fallback" else None,
    }


def request_allowance(query, system, tools, profile, output_limit=None):
    cap, reasoning, template = profile
    # Allow for the SDK's serialization and chat template. The transport
    # still checks the actual wire estimate immediately before sending.
    request_bytes = len(json.dumps([query, system, tools], ensure_ascii=True).encode())
    input_bound = request_bytes + template + 512 + 128 * len(tools)
    return CallEstimate(input_bound, output_limit or cap, reasoning).tenths, input_bound
