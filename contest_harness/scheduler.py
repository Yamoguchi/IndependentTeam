"""Schedule task sessions under the shared deadline and proxy budget."""

import asyncio
import fcntl
import json
import os
from dataclasses import asdict
from pathlib import Path
from time import monotonic

from contest_harness.agent.recovery import Recovery
from contest_harness.agent.solve import run_task
from contest_harness.planning.admission import check_admission, skipped_resolution
from contest_harness.planning.feasibility import Resolution, resolve
from contest_harness.planning.preflight import prepare_plan
from contest_harness.planning.retry import prepare_retry
from contest_harness.settings import MAX_SESSIONS, RUN_TIMEOUT
from contest_harness.telemetry.trace import RunTrace, describe
from contest_harness.telemetry.usage import Usage
from contest_harness.workspace.task_input import discover_tasks

MAX_CONSECUTIVE_PROXY_ERRORS = 3
STOP_PROXY_STATUSES = {401, 402, 403, 429}


def emit(event: dict) -> None:
    print(json.dumps(event, ensure_ascii=False), flush=True)


async def run(
    model, model_name: str, root: Path, trace: RunTrace, mode: str = "baseline", generation_limits=None,
    *, direct_publish=False, router=None, models=None, planner=None, history=None, sessions=1, run_timeout=RUN_TIMEOUT,
    early_stop=False, result_stream=None, options=None,
) -> int:
    if not 1 <= sessions <= MAX_SESSIONS:
        raise ValueError(f"--sessions must be between 1 and {MAX_SESSIONS}.")
    if sessions > 1 or early_stop:
        if planner is not None and planner != "F":
            raise ValueError("Multiple sessions and early stopping require planner F.")
        planner = "F"
    if not 0 < run_timeout <= RUN_TIMEOUT:
        raise ValueError(f"--run-timeout must be greater than 0 and at most {RUN_TIMEOUT} seconds.")
    loop = asyncio.get_running_loop()
    start = loop.time()
    deadline = start + run_timeout
    tasks = discover_tasks(root)
    if trace.enabled:
        trace.emit("run.tasks_discovered", directories=[path.name for path in tasks])
    if not tasks:
        trace.emit("run.finished", status="no_tasks", tasks=0, **trace.usage.report())
        raise ValueError("No task directories with task.json found.")
    # Clear stale canonical answers before any task can fail or exhaust the proxy.
    for task_dir in tasks:
        answer = task_dir / "answer.txt"
        if answer.is_symlink() or answer.is_file():
            answer.unlink()
    submitted = 0
    attempted = 0
    skipped = 0
    active = 0
    peak_active = 0
    retried = 0
    deferred = {}
    proxy_failed = False
    proxy_failures = set()
    proxy_blocked = False
    consecutive_proxy_errors = 0
    run_status = "interrupted"
    changed = asyncio.Event()

    def record_resolution(task_dir, task_id, resolution, usage, *, final=True):
        trace.emit(
            "recognition.outcome", directory=task_dir.name, task_id=task_id, final=final,
            resolution=asdict(resolution), usage=usage, budget=trace.budget_report(),
        )
        if early_stop and not trace.enabled:
            evidence = {key: value for key, value in resolution.evidence.items() if key != "path"}
            if "path" in resolution.evidence:
                evidence["path_sha256"] = describe(resolution.evidence["path"])["sha256"]
            emit({"event": "recognition", "directory": task_dir.name, "task_id": task_id, "final": final,
                  "resolution": {**asdict(resolution), "evidence": evidence}, "usage": usage})

    def claim(retry_round):
        nonlocal skipped
        constrained = []
        # Only the event loop touches pending. A claim removes the directory before
        # the worker can yield or another worker can inspect the queue.
        for planned_index, (task_dir, plan) in tuple(pending.items()):
            retry = deferred[planned_index] if retry_round else None
            if plan:
                budget = trace.budget_report()
                remaining = budget["remaining_tenths"]
                required = retry.required if retry else plan.admission_tenths
                admission = check_admission(
                    plan, required, remaining, budget["in_flight_tenths"],
                    ranked=planner == "F", proxy_blocked=proxy_blocked,
                    allow_local_after_proxy_failure=sessions > 1 or early_stop or options is not None, active=bool(active),
                )
                if admission.wait:
                    continue
                reason = admission.reason
                if reason == "insufficient_task_budget" and not retry_round:
                    minimum = plan.forecast.get("minimum_first_reserve_tenths")
                    if minimum is not None and minimum <= remaining + budget["in_flight_tenths"]:
                        constrained.append((planned_index, task_dir, plan, minimum))
                        continue
                trace.emit(
                    "recognition.retry_admission" if retry else "planning.decision",
                    directory=task_dir.name, task_id=plan.task_id, planned_index=planned_index,
                    admitted=reason is None, reason=reason, admission_tenths=required,
                    measured_tenths=trace.usage.measured_tenths, held_tenths=trace.usage.held_tenths, **budget,
                )
                if reason:
                    del pending[planned_index]
                    if retry:
                        record_resolution(task_dir, plan.task_id, retry.resolution, retry.current.usage.report())
                        continue
                    skipped += 1
                    if planner == "F" or options is not None:
                        resolution = skipped_resolution(plan, reason, required, remaining)
                        if options is not None and plan.prerequisite_stop:
                            resolution = Resolution("NEED_MORE_INFO", resolution.reason, resolution.evidence)
                        record_resolution(task_dir, plan.task_id, resolution, Usage().report())
                    continue
            del pending[planned_index]
            return planned_index, task_dir, plan, retry
        if constrained and not active:
            planned_index, task_dir, plan, minimum = constrained[0]
            del pending[planned_index]
            trace.emit(
                "planning.decision", directory=task_dir.name, task_id=plan.task_id, planned_index=planned_index,
                admitted=True, reason=None, admission_policy="first_request_only",
                admission_tenths=minimum, measured_tenths=trace.usage.measured_tenths,
                held_tenths=trace.usage.held_tenths, **trace.budget_report(),
            )
            return planned_index, task_dir, plan, None

    async def worker(session_id, retry_round=False):
        nonlocal attempted, submitted, active, peak_active, retried
        nonlocal proxy_failed, proxy_blocked, consecutive_proxy_errors
        while pending and loop.time() < deadline:
            if proxy_blocked and not planner:
                return
            claimed = claim(retry_round)
            if claimed is None:
                if not pending:
                    break
                changed.clear()
                await changed.wait()
                continue
            planned_index, task_dir, plan, retry = claimed
            attempted += not retry_round
            retried += retry_round
            active += 1
            peak_active = max(peak_active, active)
            started_at = monotonic()
            try:
                with trace.task(
                    retry.current.index if retry else attempted, task_dir.name, task_id=plan.task_id if plan else None,
                    session_id=session_id, wait_for_budget=sessions > 1, resume=retry.current if retry else None,
                ) as current:
                    event = None
                    recovery = retry.recovery if retry else Recovery() if early_stop else None
                    try:
                        event = await run_task(
                            model, task_dir, current, trace, mode, generation_limits,
                            direct_publish=direct_publish, router=router, models=models,
                            plan=plan, use_planned_route=planner == "F", recovery=recovery,
                            retry_query=retry.query if retry else None, options=options,
                        )
                        if event["status"] == "proxy_error":
                            proxy_failed = True
                            proxy_failures.add(task_dir)
                            consecutive_proxy_errors += 1
                            proxy_blocked |= (
                                (sessions == 1 and not early_stop and options is None)
                                or event.get("http_status") in (STOP_PROXY_STATUSES - {429} if early_stop else STOP_PROXY_STATUSES)
                                or consecutive_proxy_errors >= MAX_CONSECUTIVE_PROXY_ERRORS
                            )
                        elif event["status"] == "submitted" and event["measured_calls"]:
                            consecutive_proxy_errors = 0
                            proxy_failures.discard(task_dir)
                    finally:
                        if event is None:
                            task = plan.task if plan and plan.task else {}
                            event = {
                                "event": "task", "task_id": current.task_id,
                                "task_type": task.get("task_type"), "reward": task.get("reward"),
                                "status": "submitted" if current.published else "interrupted",
                                "seconds": round(monotonic() - started_at, 3), **current.usage.report(),
                            }
                        submitted += event["status"] == "submitted"
                        if retry:
                            event["seconds"] = round(retry.seconds + event["seconds"], 3)
                        if planner == "F" or options is not None:
                            progress = recovery.progress if recovery else None
                            evidence = {key: event[key] for key in ("error_type", "http_status") if key in event}
                            if progress and progress.context:
                                evidence["recent_tool_failures"] = [
                                    {"tool": item.tool, "status": item.status}
                                    for item in list(progress.context.errors)[-3:]
                                ]
                            resolution = resolve(
                                event["status"], signal=progress.signal if progress else None,
                                rejection=bool(progress and progress.rejection), errors=evidence,
                            )
                            if early_stop and not retry_round and event["status"] != "interrupted" and not proxy_blocked:
                                next_attempt = prepare_retry(
                                    plan, current, recovery, resolution, event["seconds"], trace, mode, generation_limits,
                                )
                                if next_attempt is not None:
                                    deferred[planned_index] = next_attempt
                            will_retry = not retry_round and planned_index in deferred
                            record_resolution(task_dir, current.task_id, resolution, current.usage.report(), final=not will_retry)
                            event.update(resolution_status=resolution.status, resolution_reason=resolution.reason,
                                         deferred_retry=will_retry)
                        task_event = {**event, "session_id": session_id, "task_index": current.index}
                        if result_stream is not None:
                            descriptor = result_stream.fileno()
                            fcntl.flock(descriptor, fcntl.LOCK_EX)
                            try:
                                result_stream.write(json.dumps({"run_id": trace.run_id, **task_event}, ensure_ascii=True) + "\n")
                                result_stream.flush()
                                os.fsync(descriptor)
                            finally:
                                fcntl.flock(descriptor, fcntl.LOCK_UN)
                        emit(task_event)
                        trace.emit(
                            "task.finished",
                            **({"budget_after": trace.budget_report()} if plan else {}),
                            **{key: value for key, value in event.items() if key != "event"},
                        )
            finally:
                active -= 1
                changed.set()

    try:
        if planner:
            plans = prepare_plan(
                tasks, model_name, trace, mode, generation_limits, direct_publish, router, planner, history,
                early_stop, options=options,
            )
            queue = [(plan.directory, plan) for plan in plans]
        else:
            queue = [(directory, None) for directory in tasks]
        pending = dict(enumerate(queue, 1))
        try:
            async with asyncio.timeout_at(deadline):
                async with asyncio.TaskGroup() as workers:
                    for session_id in range(1, min(sessions, len(tasks)) + 1):
                        workers.create_task(worker(session_id), name=f"harness-session-{session_id}")
                if deferred:
                    pending = {index: queue[index - 1] for index in sorted(deferred)}
                    async with asyncio.TaskGroup() as workers:
                        for session_id in range(1, min(sessions, len(pending)) + 1):
                            workers.create_task(worker(session_id, retry_round=True), name=f"harness-retry-{session_id}")
        except TimeoutError:
            run_status = "deadline"
        else:
            run_status = (
                "deadline" if pending and loop.time() >= deadline else
                "proxy_error" if (proxy_failures if early_stop else proxy_failed) else
                "budget_exceeded" if trace.budget_report()["local_overrun_tenths"] else
                "completed" if submitted == len(tasks) else
                "budget_exceeded" if trace.usage.denied_calls else "partial"
            )
    finally:
        summary = {
            "model": model_name,
            "tasks": len(tasks),
            "submitted": submitted,
            "seconds": round(loop.time() - start, 3),
            "sessions": sessions,
            "peak_active_sessions": peak_active,
            "retried_tasks": retried,
            **trace.usage.report(),
            **trace.budget_report(),
        }
        emit({"event": "summary", **summary})
        trace.emit(
            "run.finished",
            status=run_status,
            attempted=attempted,
            unattempted=len(tasks) - attempted,
            **({"planning_skipped": skipped} if planner else {}),
            **summary,
        )
    return (
        0
        if (submitted == len(tasks) and trace.usage.calls == trace.usage.measured_calls
            and not trace.budget_report()["local_overrun_tenths"])
        else 1
    )
