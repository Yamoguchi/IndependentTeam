"""Evidence and bounded retry policy for planner G."""

import re
from dataclasses import dataclass, field
from stat import S_ISDIR, S_ISREG

from contest_harness.planning.routing import DEFAULT_LAMBDA
from contest_harness.workspace.paths import InvalidToolCall, safe_path

REFUSALS = {"UNSOLVABLE_CONFIRMED", "UNSOLVABLE_SUSPECTED"}
SIGNALS = {"UNSOLVABLE_SUSPECTED", "NEED_MORE_INFO"}
SIGNAL_PROMPT = (
    "\nIf you cannot proceed because specific information must be supplied, end with NEED_MORE_INFO alone. "
    "If the task may be impossible because a prerequisite is unavailable, end with UNSOLVABLE_SUSPECTED alone. "
    "Use submit_answer only for a valid final answer."
)
# Only recognize standalone, unconditional declarations about existing inputs.
REQUIRED_INPUT = re.compile(
    r"^[ \t]*(?:required (?:existing )?input (file|directory)\s*:\s*"
    r"|(read|use) the (?:required|supplied|provided) (file|directory)\s+)"
    r"`([\w./-]{1,240})`\.?[ \t]*$",
    re.I | re.M,
)
ALTERNATIVES = re.compile(
    r"\b(?:if|unless|otherwise|alternativ\w*|fallback|optional|instead|"
    r"or|either|when|may|example|missing|unavailable|absent|reconstruct\w*|generat\w*|creat\w*)\b",
    re.I,
)
RETRY_PRIORS = {
    "proxy_transient": 0.5,
    "candidate_rejected": 0.25,
    "model_step_limit": 0.2,
    "model_generation_failed": 0.2,
    "model_no_submission": 0.2,
}


@dataclass(frozen=True)
class Resolution:
    status: str
    reason: str
    evidence: dict = field(default_factory=dict)


def check_prerequisites(instruction, workspace):
    """Check literal prerequisites. Ambiguous instructions go through ReAct."""
    matches = list(REQUIRED_INPUT.finditer(instruction))
    if not matches or "```" in instruction or "~~~" in instruction:
        return None
    prose = re.sub(r"`[^`\n]*`", "", instruction)
    if ALTERNATIVES.search(prose):
        return None
    for match in matches:
        label, _, verb_kind, name = match.groups()
        kind = (label or verb_kind).lower()
        try:
            path = safe_path(workspace, name)
        except (InvalidToolCall, OSError):
            # Tool restrictions or ambiguous paths are not evidence of absence.
            continue
        try:
            mode = path.stat().st_mode
        except (FileNotFoundError, NotADirectoryError):
            return Resolution("UNSOLVABLE_CONFIRMED", "required_input_absent", {
                "path": name, "required_kind": kind, "observation": "path_does_not_exist",
            })
        except OSError:
            continue
        if not (S_ISREG(mode) if kind == "file" else S_ISDIR(mode)):
            return Resolution("UNSOLVABLE_CONFIRMED", "required_resource_wrong_type", {
                "path": name, "required_kind": kind,
                "observation": "directory" if S_ISDIR(mode) else "regular_file" if S_ISREG(mode) else "special_file",
            })
    return None


def resolve(status, *, signal=None, rejection=False, errors=None):
    evidence = {"attempt_status": status, **(errors or {})}
    if status == "submitted":
        return Resolution("DONE", "answer_published", evidence)
    if status == "budget_exceeded":
        return Resolution("BUDGET_EXCEEDED", "llm_reservation_denied", evidence)
    if status == "proxy_error":
        code = evidence.get("http_status")
        transient = code is None or code in {408, 429} or 500 <= code <= 599
        return Resolution("FAILED", "proxy_transient" if transient else "proxy_request_failed", evidence)
    if status == "task_error":
        return Resolution("FAILED", "harness_task_error", evidence)
    if status == "generation_failed":
        return Resolution("FAILED", "model_generation_failed", evidence)
    if status == "interrupted":
        return Resolution("FAILED", "attempt_interrupted", evidence)
    if signal in SIGNALS and status == "no_answer":
        reason = "model_reported_missing_information" if signal == "NEED_MORE_INFO" else "model_suspected_missing_prerequisite"
        return Resolution(signal, reason, {**evidence, "model_signal": signal})
    if rejection:
        return Resolution("UNCERTAIN", "candidate_rejected", {**evidence, "validation_rejected": True})
    reason = "model_step_limit" if status == "step_limit" else "model_no_submission"
    return Resolution("UNCERTAIN", reason, evidence)


def retry_decision(resolution, reward, cost_tenths, remaining_tenths, penalty=DEFAULT_LAMBDA):
    probability = RETRY_PRIORS.get(resolution.reason)
    gain = probability * reward - penalty * (cost_tenths / 10) if probability is not None else None
    reason = (
        "ineligible_outcome" if gain is None else
        "no_positive_gain" if gain <= 0 else
        "unknown_reserve" if remaining_tenths is None else
        "insufficient_reserve" if remaining_tenths < cost_tenths else "positive_gain"
    )
    return {
        "eligible": reason == "positive_gain", "reason": reason, "outcome": resolution.reason,
        "probability_prior": probability, "expected_gain": gain, "penalty": penalty,
        "required_tenths": cost_tenths, "remaining_tenths": remaining_tenths,
    }
