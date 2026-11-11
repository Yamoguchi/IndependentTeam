"""Deterministic routing and evidence-based, optional escalation."""

import json
import os
import re
from dataclasses import asdict, dataclass
from math import ceil, isfinite
from pathlib import Path

from contest_harness.workspace.evidence import SOURCE_TYPES, test_file
from contest_harness.workspace.paths import InvalidToolCall, safe_path
from contest_harness.workspace.sandbox import CONTROL_FILES

LOCAL_METHODS = {
    "math": {"integer_expression"},
    "data_analysis": {"csv_sum", "csv_filtered_sum"},
    "search": {"labelled_field"},
}
OPERATIONS = {
    "calculate": r"\b(?:calculate|compute|evaluate|divide|round|multiply)\b",
    "filter": r"\b(?:where|filter|between|inclusive)\b",
    "join": r"\b(?:join|merge|cross.reference|employer|across files)\b",
    "aggregate": r"\b(?:sum|aggregate|group|average|median)\b",
    "edit": r"\b(?:fix|repair|implement|edit|patch|modify)\b",
    "test": r"\b(?:tests?|verify|pytest|unittest)\b|\btest_",
}
SCAN_LIMIT = 128
DEFAULT_LAMBDA = 0.00025  # Points per output-equivalent token.
DEFAULT_CHEAP_STEPS = 8
ESCALATION_PRIORS = {"candidate_rejected": 0.4, "generation_failed": 0.3, "step_limit": 0.2}


@dataclass(frozen=True)
class ModelEvidence:
    model: str
    tasks: int
    solved: int
    mean_cost: float
    after_failure_tasks: int
    after_failure_solved: int

    def __post_init__(self):
        counts = (self.tasks, self.solved, self.after_failure_tasks, self.after_failure_solved)
        if (
            any(type(n) is not int or n < 0 for n in counts)
            or not self.tasks >= self.solved >= self.after_failure_solved
            or not self.tasks >= self.after_failure_tasks >= self.after_failure_solved
            or self.solved - self.after_failure_solved > self.tasks - self.after_failure_tasks
            or self.tasks == 0
            or type(self.mean_cost) not in {int, float}
            or not isfinite(self.mean_cost)
            or self.mean_cost < 0
        ):
            raise ValueError("Invalid model evidence in routing profile.")

    @property
    def success_rate(self):
        return self.solved / self.tasks

    def conditional_success(self, outcome):
        # Matched fresh-run results are a proxy for handoff trials. Smooth sparse
        # observations toward a prior that depends on the observed outcome.
        prior = ESCALATION_PRIORS[outcome]
        return (self.after_failure_solved + 2 * prior) / (self.after_failure_tasks + 2)


def load_profile(path, cheap_model, allowed_models):
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw["schema_version"] != 1 or raw["cheap_model"] != cheap_model:
        raise ValueError("Routing profile must match the configured initial model.")
    groups = {}
    for kind, rows in raw["by_type"].items():
        models = {row["model"]: ModelEvidence(**row) for row in rows}
        if (
            cheap_model not in models or not models.keys() <= allowed_models
            or len(models) != len(rows) or len({row.tasks for row in models.values()}) != 1
            or len({row.after_failure_tasks for row in models.values()}) != 1
            or models[cheap_model].after_failure_tasks != models[cheap_model].tasks - models[cheap_model].solved
            or models[cheap_model].after_failure_solved
        ):
            raise ValueError("Routing profile must use matched tasks and approved models.")
        groups[kind] = models
    return groups


def features_for(task, workspace):
    count = 0
    total_bytes = 0
    suffixes = set()
    sources = 0
    scanned = 0
    for parent, dirs, files in os.walk(workspace):
        dirs[:] = sorted(name for name in dirs if not (Path(parent) / name).is_symlink())
        remaining = SCAN_LIMIT - scanned
        scanned += len(dirs) + len(files) + 1
        for name in sorted(files)[:remaining]:
            path = Path(parent) / name
            if name in CONTROL_FILES or path.is_symlink() or not path.is_file():
                continue
            total_bytes += path.stat().st_size
            suffixes.add(path.suffix[:20])
            sources += path.suffix in SOURCE_TYPES and not test_file(path.relative_to(workspace))
            count += 1
        if scanned >= SCAN_LIMIT:
            break
    instruction = task["instruction"]
    operations = [name for name, pattern in OPERATIONS.items() if re.search(pattern, instruction, re.I)]
    complexity = (
        int(len(instruction) > 650)
        + int(count > 16 or total_bytes > 64 * 1024 or scanned >= SCAN_LIMIT)
        + int(len(operations) >= 3)
        + int("join" in operations)
        + int(task["task_type"] == "coding") * (1 + int(sources > 1))
    )
    return {
        "instruction_chars": len(instruction), "files_seen": count, "file_bytes_seen": total_bytes,
        "file_types": sorted(suffixes), "scan_limited": scanned >= SCAN_LIMIT,
        "operations": operations, "complexity": complexity,
    }


@dataclass(frozen=True)
class Route:
    name: str
    model: str | None
    reason: str
    features: dict
    cheap: ModelEvidence | None = None
    smart: ModelEvidence | None = None

    def report(self):
        return asdict(self)


@dataclass(frozen=True)
class TaskRouter:
    cheap_model: str
    profiles: dict
    cascade: bool
    penalty: float = DEFAULT_LAMBDA
    cheap_steps: int = DEFAULT_CHEAP_STEPS

    def select(self, task, workspace, reference, remaining):
        features = features_for(task, workspace)
        kind = task["task_type"]
        if reference and reference[1] in LOCAL_METHODS.get(kind, ()):
            return Route("LOCAL", None, reference[1], features)
        profiles = self.profiles.get(kind, {})
        cheap = profiles.get(self.cheap_model)
        # Prefer accuracy, then lower cost on the same independently scored tasks.
        best = min(profiles.values(), key=lambda row: (-row.success_rate, row.mean_cost, row.model), default=None)
        smart = best if best is not None and cheap is not None and (
            best.success_rate > cheap.success_rate
            or best.success_rate == cheap.success_rate and best.mean_cost < cheap.mean_cost
        ) else None
        if smart and features["complexity"] >= 2:
            lift = (smart.success_rate - cheap.success_rate) * task["reward"]
            future_cost = smart.mean_cost - cheap.mean_cost
            if lift > DEFAULT_LAMBDA * future_cost and (remaining is None or remaining >= ceil(smart.mean_cost * 10)):
                return Route("SMART_LLM", smart.model, "complex_with_calibrated_model", features, cheap, smart)
        if self.cascade and smart:
            return Route("CASCADE", self.cheap_model, "initial_attempt_with_escalation_option", features, cheap, smart)
        return Route("CHEAP_LLM", self.cheap_model, "initial_model", features, cheap, smart)

    def escalation(self, route, outcome, reward, remaining, first_reserve_tenths, handoff_bytes):
        smart = route.smart
        if outcome not in ESCALATION_PRIORS:
            return {"escalate": False, "reason": "no_eligible_terminal_outcome", "outcome": outcome}
        probability = smart.conditional_success(outcome)
        smart_cost = smart.mean_cost + handoff_bytes / 10
        verify_cost = 0  # The verifier is local and makes no LLM calls.
        gain = probability * reward - self.penalty * (smart_cost + verify_cost)
        reserve = max(first_reserve_tenths, ceil((smart_cost + verify_cost) * 10))
        reason = (
            "no_positive_gain" if gain <= 0 else
            "unknown_reserve" if remaining is None else
            "insufficient_reserve" if remaining < reserve else "positive_gain"
        )
        return {
            "escalate": reason == "positive_gain", "reason": reason, "outcome": outcome,
            "p_smart": probability, "reward": reward, "lambda": self.penalty, "expected_gain": gain,
            "c_smart": smart_cost, "c_verify": verify_cost, "required_tenths": reserve,
            "remaining_tenths": remaining, "from_model": route.model, "to_model": smart.model,
        }


def file_excerpt(workspace, name, limit=900):
    try:
        path = safe_path(workspace, name)
        if path.is_file():
            with path.open("rb") as source:
                data = source.read(limit + 1)
            text = data[:limit].decode("utf-8", errors="ignore")
            if "\0" not in text:
                return {"path": name, "text": text, "truncated": len(data) > limit}
    except (OSError, InvalidToolCall):
        pass
    return None
