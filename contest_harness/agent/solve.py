"""Select an attempt and escalate when the task route permits it."""

import asyncio
from functools import partial
from pathlib import Path
from time import monotonic

from openjiuwen.core.common.exception.errors import BaseError

from contest_harness.agent.generation import GenerationFailed
from contest_harness.agent.react import run_attempt
from contest_harness.agent.recovery import AttemptProgress, edit_state, handoff_query
from contest_harness.agent.submission import AnswerPublisher
from contest_harness.planning.feasibility import SIGNAL_PROMPT
from contest_harness.planning.forecast import request_allowance
from contest_harness.planning.preflight import initial_steps
from contest_harness.planning.routing import ESCALATION_PRIORS, Route
from contest_harness.settings import COMPACT_MODES, MAX_STEPS
from contest_harness.telemetry.trace import RunTrace, TaskTrace, describe, error_fields
from contest_harness.telemetry.usage import BudgetExceeded
from contest_harness.workspace.answers import AnswerFormat
from contest_harness.workspace.evidence import CodingEvidence
from contest_harness.workspace.files import workspace_for
from contest_harness.workspace.paths import safe_path
from contest_harness.workspace.reference import compute_reference
from contest_harness.workspace.task_input import load_task
from contest_harness.workspace.tools import run_python_file, toolset


async def solve(
    model, task_dir: Path, task: dict, current: TaskTrace, trace: RunTrace, mode: str = "baseline",
    generation_limits=None, *, direct_publish=False, router=None, models=None, plan=None, use_planned_route=False,
    recovery=None, retry_query=None,
) -> str:
    workspace = workspace_for(task_dir)
    trace.emit(
        "workspace.ready", workspace="task" if workspace == task_dir else "workspace"
    )

    instruction = task["instruction"]
    answer_format = AnswerFormat.infer(instruction)
    reference = (plan.reference if plan and router and plan.preflight_error is None else
                 compute_reference(instruction, workspace, answer_format.kind))
    compact = mode in COMPACT_MODES

    async def run_original_test(name):
        return await asyncio.to_thread(run_python_file, workspace, safe_path(workspace, name), compact=compact)

    coding = recovery.coding if recovery and recovery.coding else (
        CodingEvidence(workspace, instruction, run_original_test) if task["task_type"] == "coding" else None
    )
    if recovery is not None:
        recovery.coding = coding
    publisher = AnswerPublisher(task_dir, instruction, answer_format, coding, reference, current, trace)

    route = (
        plan.route if use_planned_route and plan and plan.preflight_error is None else
        router.select(task, workspace, reference, trace.budget_report()["remaining_tenths"])
        if router else Route("BASELINE", model.model_config.model_name, "control", {})
    )
    trace.emit("route.selected", route=route.report(), **trace.budget_report())
    if route.name == "LOCAL":
        if await publisher.publish(AttemptProgress(), reference[0], "local"):
            return "submitted"
        route = router.select(task, workspace, None, trace.budget_report()["remaining_tenths"])
        trace.emit("route.fallback", route=route.report(), reason="local_candidate_rejected")

    async def attempt(active_model, active_route, query, stage, definitions, system):
        progress = AttemptProgress()
        if recovery is not None:
            recovery.progress = progress
            system += SIGNAL_PROMPT
        calls_before = current.usage.calls
        step_limit = MAX_STEPS if stage == "escalated" else initial_steps(router, route)
        status = "interrupted"
        trace.emit(
            "route.attempt_started", stage=stage, model=active_model.model_config.model_name, step_limit=step_limit,
        )
        try:
            status = await run_attempt(
                active_model, task_dir, workspace, task, current, trace, mode, generation_limits,
                direct_publish, coding, answer_format, reference, definitions, system, query,
                progress, partial(publisher.publish, progress), calls_before, step_limit,
                active_route == "CASCADE" or recovery is not None, recovery is not None,
            )
        except GenerationFailed as error:
            status = "generation_failed"
            if router is None:
                raise
            trace.emit("route.generation_failed", **error_fields(error))
        except BudgetExceeded:
            status = "budget_exceeded"
            raise
        except (BaseError, TimeoutError):
            status = "budget_exceeded" if current.usage.denied_calls else "proxy_error"
            raise
        finally:
            if trace.enabled:
                candidate = progress.candidate
                if candidate is None and progress.context is not None:
                    candidate = progress.context.candidate
                trace.emit(
                    "route.attempt_finished", stage=stage, status=status, rejection=progress.rejection,
                    candidate=describe(candidate) if candidate is not None else None,
                    workspace=edit_state(coding) if router else None,
                )
        return status, progress

    definitions, system = toolset(task["task_type"], mode, route.name, route.features)
    first = models[route.model] if router else model
    status, progress = await attempt(
        first, route.name, retry_query or instruction, "retry" if retry_query else "initial", definitions, system,
    )
    if route.name != "CASCADE" or status == "submitted":
        return status

    outcome = "candidate_rejected" if progress.rejection else status
    if (outcome in {"no_answer", "step_limit"} and progress.context.errors
            and all(item.status != "ok" for item in progress.context.results.values())):
        outcome = "tool_errors_without_progress"
    remaining = trace.budget_report()["remaining_tenths"]
    if outcome in ESCALATION_PRIORS:
        from contest_harness.agent.model_profiles import PROFILES

        progress.context.remaining_tenths = remaining
        query = handoff_query(instruction, workspace, progress, coding, outcome)
        definitions, system = toolset(task["task_type"], mode, "SMART_LLM", route.features)
        profile = PROFILES[route.smart.model]
        limit = None
        if generation_limits:
            limit = min(profile[0], generation_limits["code" if task["task_type"] == "coding" else "analysis"])
        reserve, _ = request_allowance(
            query, system + (SIGNAL_PROMPT if recovery is not None else ""), definitions, profile, limit,
        )
        handoff_bytes = len(query[len(instruction):].encode("utf-8", errors="replace"))
        decision = router.escalation(route, outcome, task["reward"], remaining, reserve, handoff_bytes)
    else:
        decision = router.escalation(route, outcome, task["reward"], remaining, 0, 0)
    trace.emit("route.escalation_decision", **decision)
    if decision["escalate"]:
        if trace.enabled:
            trace.emit("route.escalated", reason=outcome, workspace=edit_state(coding), handoff=describe(query))
        status, _ = await attempt(models[route.smart.model], "SMART_LLM", query, "escalated", definitions, system)
    return status


async def run_task(
    model, task_dir: Path, current: TaskTrace, trace: RunTrace, mode: str = "baseline",
    generation_limits=None, *, direct_publish=False, router=None, models=None, plan=None, use_planned_route=False,
    recovery=None, retry_query=None,
) -> dict:
    started_at = monotonic()
    task = {}
    error_details = {}
    try:
        task = plan.task if plan and plan.task is not None else load_task(task_dir / "task.json")
        current.task_id = task["task_id"]
        if trace.enabled:
            trace.emit(
                "task.loaded",
                task_type=task["task_type"],
                reward=task["reward"],
                instruction=describe(task["instruction"]),
            )
        status = await solve(
            model, task_dir, task, current, trace, mode, generation_limits,
            direct_publish=direct_publish, router=router, models=models,
            plan=plan, use_planned_route=use_planned_route, recovery=recovery, retry_query=retry_query,
        )
    except BudgetExceeded:
        status = "budget_exceeded"
    except GenerationFailed as error:
        status = "generation_failed"
        error_details = error_fields(error)
    except (BaseError, TimeoutError) as error:
        status = "budget_exceeded" if current.usage.denied_calls else "proxy_error"
        error_details = error_fields(error)
    except (KeyError, ValueError, OSError, TypeError, RecursionError) as error:
        status = "task_error"
        error_details = error_fields(error)
        trace.emit("task.error", **error_details)

    if current.published:
        status = "submitted"
    return {
        "event": "task",
        "task_id": task.get("task_id", task_dir.name),
        "task_type": task.get("task_type"),
        "reward": task.get("reward"),
        "status": status,
        "seconds": round(monotonic() - started_at, 3),
        **current.usage.report(),
        **error_details,
    }
