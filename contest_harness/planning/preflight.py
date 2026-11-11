"""Build and rank task plans before starting model sessions."""

from dataclasses import dataclass
from pathlib import Path
from time import monotonic

from contest_harness.planning.feasibility import SIGNAL_PROMPT, Resolution, check_prerequisites
from contest_harness.planning.forecast import PlanningHistory, estimate_task, request_allowance, settings_for
from contest_harness.planning.routing import Route, features_for
from contest_harness.settings import MAX_STEPS, MIN_ADAPTIVE_OUTPUT
from contest_harness.telemetry.trace import describe
from contest_harness.workspace.answers import AnswerFormat
from contest_harness.workspace.files import workspace_for
from contest_harness.workspace.reference import compute_reference
from contest_harness.workspace.task_input import load_task
from contest_harness.workspace.tools import toolset


def initial_steps(router, route):
    return router.cheap_steps if route.smart and route.name in {"CHEAP_LLM", "CASCADE"} else MAX_STEPS


@dataclass
class TaskPlan:
    directory: Path
    input_index: int
    task: dict | None = None
    route: Route | None = None
    reference: tuple | None = None
    forecast: dict | None = None
    priority: float | None = None
    instruction_sha256: str | None = None
    reference_method: str | None = None
    preflight_ms: float = 0
    preflight_error: str | None = None
    prerequisite_stop: Resolution | None = None
    recognition_ms: float = 0

    @property
    def task_id(self):
        return self.task["task_id"] if self.task else self.directory.name

    @property
    def admission_tenths(self) -> int | None:
        return self.forecast["admission_tenths"] if self.forecast is not None else None

    def report(self):
        return {
            "directory": self.directory.name, "input_index": self.input_index, "task_id": self.task_id,
            "task_type": self.task["task_type"] if self.task else None,
            "reward": self.task["reward"] if self.task else None,
            "instruction_sha256": self.instruction_sha256, "route": self.route.name if self.route else None,
            "model": self.route.model if self.route else None,
            "reference_method": self.reference_method,
            "route_reason": self.route.reason if self.route else None,
            "features": self.route.features if self.route else None,
            "forecast": self.forecast, "priority": self.priority, "preflight_ms": self.preflight_ms,
            "preflight_error": self.preflight_error,
        }


def prepare_plan(tasks, model_name, trace, mode, generation_limits, direct_publish, router, planner, history, early_stop=False):
    from contest_harness.agent.model_profiles import PROFILES

    started = monotonic()
    remaining = trace.budget_report()["remaining_tenths"]
    plans = []
    for index, directory in enumerate(tasks, 1):
        plan = TaskPlan(directory, index)
        try:
            plan.task = load_task(directory / "task.json")
            plan.instruction_sha256 = describe(plan.task["instruction"])["sha256"]
        except (ValueError, OSError, TypeError, RecursionError) as error:
            plan.preflight_error = type(error).__name__
        plans.append(plan)
    history = (history or PlanningHistory()).independent_of(
        {plan.task_id for plan in plans}, {plan.instruction_sha256 for plan in plans}
    )
    for plan in plans:
        if plan.task is None:
            continue
        task_started = monotonic()
        try:
            workspace = workspace_for(plan.directory)
            instruction, kind = plan.task["instruction"], plan.task["task_type"]
            if early_stop:
                checked_at = monotonic()
                plan.prerequisite_stop = check_prerequisites(instruction, workspace)
                plan.recognition_ms = round((monotonic() - checked_at) * 1000, 3)
                trace.emit("recognition.checked", directory=plan.directory.name, task_id=plan.task_id,
                           duration_ms=plan.recognition_ms)
                if plan.prerequisite_stop:
                    continue
            if router:
                answer_format = AnswerFormat.infer(instruction)
                plan.reference = compute_reference(instruction, workspace, answer_format.kind)
                plan.reference_method = plan.reference[1] if plan.reference else None
                route = router.select(plan.task, workspace, plan.reference, remaining)
                if route.name == "LOCAL" and answer_format.validate(plan.reference[0])[1].status != "confirmed":
                    route = router.select(plan.task, workspace, None, remaining)
            else:
                route = Route("BASELINE", model_name, "control", features_for(plan.task, workspace))
            plan.route = route
            settings = settings_for(
                mode, generation_limits, direct_publish, initial_steps(router, route),
                router.penalty if route.name == "CASCADE" else None,
            )
            first_reserve = input_bytes = 0
            reasoning = False
            if route.model is not None:
                definitions, system = toolset(kind, mode, route.name, route.features)
                if early_stop:
                    system += SIGNAL_PROMPT
                profile = PROFILES[route.model]
                limit = generation_limits["code" if kind == "coding" else "analysis"] if generation_limits else None
                first_reserve, input_bytes = request_allowance(instruction, system, definitions, profile, limit)
                reasoning = bool(profile[1])
            plan.forecast = estimate_task(
                kind, route, settings, history, first_reserve=first_reserve, input_bytes=input_bytes,
                reasoning=reasoning,
            )
            plan.forecast["settings"] = settings
            if generation_limits and route.model is not None:
                minimum = min(MIN_ADAPTIVE_OUTPUT, limit, profile[0])
                plan.forecast["minimum_first_reserve_tenths"], _ = request_allowance(
                    instruction, system, definitions, profile, minimum,
                )
            plan.forecast["smart_model"] = route.smart.model if route.name == "CASCADE" else None
            probability = plan.forecast["success_probability"]
            plan.priority = plan.task["reward"] / (plan.forecast["priority_denominator_tenths"] / 10)
            if probability is not None:
                plan.priority *= probability
        except (ValueError, OSError, TypeError, RecursionError) as error:
            plan.preflight_error = type(error).__name__
        finally:
            plan.preflight_ms = round((monotonic() - task_started) * 1000, 3)
    original_order = [plan.task_id for plan in plans]
    if planner == "F":
        plans.sort(key=lambda plan: (
            plan.prerequisite_stop is None, plan.priority is None, -(plan.priority or 0), plan.input_index,
        ))
    for index, plan in enumerate(plans, 1):
        trace.emit("planning.task", planned_index=index, **plan.report())
    trace.emit(
        "planning.ready", planner=planner, input_order=original_order,
        planned_order=[plan.task_id for plan in plans], duration_ms=round((monotonic() - started) * 1000, 3),
        **trace.budget_report(),
    )
    return plans
