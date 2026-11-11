"""Prepare deferred attempts using the task's previous evidence."""

from dataclasses import dataclass

from contest_harness.agent.model_profiles import PROFILES
from contest_harness.agent.recovery import Recovery, handoff_query
from contest_harness.planning.feasibility import SIGNAL_PROMPT, Resolution, retry_decision
from contest_harness.planning.forecast import request_allowance
from contest_harness.telemetry.trace import TaskTrace
from contest_harness.workspace.files import workspace_for
from contest_harness.workspace.tools import toolset


@dataclass
class DeferredTask:
    current: TaskTrace
    recovery: Recovery
    query: str
    required: int
    seconds: float
    resolution: Resolution


def prepare_retry(plan, current, recovery, resolution, seconds, trace, mode, generation_limits):
    """Recompute the first-call allowance using the actual retry prompt."""
    if not plan.forecast or not plan.route or plan.route.name in {"LOCAL", "CASCADE"}:
        return None
    required = plan.forecast["admission_tenths"]
    budget = trace.budget_report()
    remaining = budget["remaining_tenths"]
    available = remaining + budget["in_flight_tenths"] if remaining is not None else None
    decision = retry_decision(resolution, plan.task["reward"], required, available)
    if decision["eligible"]:
        instruction = plan.task["instruction"]
        progress = recovery.progress
        query = instruction
        if progress and progress.context and resolution.reason != "proxy_transient":
            progress.context.remaining_tenths = remaining
            query = handoff_query(instruction, workspace_for(plan.directory), progress, recovery.coding, resolution.reason)
        elif recovery.coding and recovery.coding.revision:
            query += "\nInspect the current source edits and rerun the required tests before submission."
        definitions, system = toolset(plan.task["task_type"], mode, plan.route.name, plan.route.features)
        limit = generation_limits["code" if plan.task["task_type"] == "coding" else "analysis"] if generation_limits else None
        reserve, _ = request_allowance(query, system + SIGNAL_PROMPT, definitions, PROFILES[plan.route.model], limit)
        required = max(reserve, required)
        decision = retry_decision(resolution, plan.task["reward"], required, available)
    trace.emit("recognition.retry_decision", **decision)
    if decision["eligible"]:
        return DeferredTask(current, recovery, query, required, seconds, resolution)
    return None
