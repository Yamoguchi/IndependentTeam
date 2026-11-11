"""Determine whether a planned task can start on the current budget."""

from dataclasses import dataclass

from contest_harness.planning.feasibility import Resolution
from contest_harness.planning.preflight import TaskPlan


@dataclass(frozen=True)
class Admission:
    reason: str | None = None
    wait: bool = False


def check_admission(
    plan: TaskPlan, required: int | None, remaining: int | None, in_flight: int, *,
    ranked: bool, proxy_blocked: bool, allow_local_after_proxy_failure: bool, active: bool,
) -> Admission:
    if plan.prerequisite_stop:
        return Admission(reason=plan.prerequisite_stop.reason)
    if proxy_blocked and not (allow_local_after_proxy_failure and plan.route and plan.route.name == "LOCAL"):
        return Admission(reason="proxy_error")
    if ranked:
        if plan.preflight_error or required is None:
            return Admission(reason="preflight_error")
        if remaining is not None and required > remaining:
            if active and required <= remaining + in_flight:
                return Admission(wait=True)
            return Admission(reason="insufficient_task_budget")
    return Admission()


def skipped_resolution(
    plan: TaskPlan, reason: str, required: int | None, remaining: int | None,
) -> Resolution:
    if plan.prerequisite_stop:
        return plan.prerequisite_stop
    if reason == "insufficient_task_budget":
        return Resolution(
            "BUDGET_EXCEEDED",
            "local_budget_exhausted" if remaining is not None and remaining <= 0 else "estimated_cost_exceeds_remaining",
            {"required_tenths": required, "remaining_tenths": remaining},
        )
    return Resolution("FAILED", "proxy_circuit_open" if reason == "proxy_error" else reason,
                      {"preflight_error": plan.preflight_error})
