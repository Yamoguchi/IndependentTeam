"""Join task metadata, recorded events, answers and independent scores."""

from collections import Counter, defaultdict

from contest_harness.evaluation.metrics import (
    LEDGER_FIELDS,
    USAGE_FIELDS,
    context_stats,
    equivalent,
    events_named,
    tool_stats,
    usage_total,
)


def task_results(
    source, manifest, events, calls, journal_usage, score_by_id, *, legacy, ledger, reliable, contiguous,
):
    usage_fields = (*USAGE_FIELDS, *LEDGER_FIELDS) if ledger else USAGE_FIELDS
    task_journal = {row["task_id"]: row for row in journal_usage}
    by_index = defaultdict(list)
    for row in events:
        if row.get("task_index") is not None:
            by_index[row["task_index"]].append(row)
    calls_by_index = defaultdict(list)
    for row in calls:
        calls_by_index[row["task_index"]].append(row)

    starts_by_directory = {
        row["directory"]: row["task_index"] for row in events_named(events, "task.started")
    }
    starts_by_id = {row["task_id"]: row["task_index"] for row in events_named(events, "task.loaded")}
    sessions_by_index = {row["task_index"]: row.get("session_id") for row in events_named(events, "task.started")}
    plans = {row["directory"]: row for row in events_named(events, "planning.task")}
    plan_decisions = {row["directory"]: row for row in events_named(events, "planning.decision")}
    resolutions = {row["directory"]: row for row in events_named(events, "recognition.outcome")}
    published = set(manifest.get("journal", {}).get("published_answer_directories", []))
    rows = []
    task_root = source / "tasks"
    for index, metadata in enumerate(manifest["input"]["tasks"], 1):
        task_id = metadata["task_id"]
        directory = metadata["directory"]
        order = starts_by_directory.get(directory, starts_by_id.get(task_id))
        task_events = by_index[order]
        task_calls = calls_by_index[order]
        task_finishes = events_named(task_events, "task.finished")
        finished = task_finishes[-1] if task_finishes else None
        journal = task_journal.get(task_id) or finished
        started = bool(task_events or journal)
        # A published answer may survive a kill just before the answer.saved event.
        if directory is not None:
            answer = task_root / directory / "answer.txt"
            if answer.is_file() and not answer.is_symlink() and not answer.parent.is_symlink():
                published.add(directory)
        validations = events_named(task_events, "answer.validated")
        publications = events_named(task_events, "answer.saved")
        validation = validations[-1] if validations else None
        route_events = [row for row in task_events if row["event"] in {"route.selected", "route.fallback"}]
        route = route_events[-1]["route"] if route_events else None
        initial_route = route_events[0]["route"] if route_events else None
        initial_attempt = next(iter(events_named(task_events, "route.attempt_started")), None)
        plan, admission = plans.get(directory), plan_decisions.get(directory)
        forecast = (plan or {}).get("forecast") or {}
        stop_reason = admission.get("reason") if admission else None
        budget_after = (finished or {}).get("budget_after")
        escalation = next(iter(events_named(task_events, "route.escalated")), None)
        decision = next(iter(events_named(task_events, "route.escalation_decision")), None)
        attempts = events_named(task_events, "route.attempt_finished")
        submitted = bool(
            publications
            or (journal and journal["status"] == "submitted")
            or directory in published
        )
        task_scores = score_by_id.get(task_id, {})
        resolution_event = resolutions.get(directory)
        resolution = (resolution_event or {}).get("resolution")
        if submitted and resolution and resolution["status"] != "DONE":
            resolution = {"status": "DONE", "reason": "answer_published", "evidence": {"journal_recovery": True}}
        observed_task = usage_total(task_calls, ledger=ledger)
        task_usage = usage_total(
            [journal] if legacy and journal else task_calls,
            reliable=reliable
            or bool(
                journal and (legacy or all(observed_task[key] == journal.get(key) for key in usage_fields))
            ),
            ledger=ledger,
        )
        seconds = journal.get("seconds") if journal else None
        local_ms = (
            round(max(0, seconds * 1000 - sum(row.get("duration_ms") or 0 for row in task_calls)), 3)
            if seconds is not None and events and all(row.get("duration_ms") is not None for row in task_calls)
            else None
        )
        actual_cost = task_usage["output_equivalent"] if started else None
        estimated_cost = equivalent(forecast.get("estimated_tenths"))
        context = context_stats(task_events, task_calls) if task_events else None
        rows.append(
            {
                **metadata,
                "input_index": index,
                "task_index": order,
                "session_id": sessions_by_index.get(order),
                "started": started,
                "submitted": submitted,
                "answer_validation": {
                    key: validation[key] for key in ("source", "accepted", "format_kind", "format", "semantic")
                } if validation else None,
                "publication_source": publications[-1].get("source") if publications else None,
                "status": (
                    journal["status"]
                    if journal
                    else "submitted"
                    if submitted
                    else "interrupted"
                    if started
                    else "skipped"
                    if stop_reason
                    else "not_started"
                ),
                "passed": task_scores.get("passed"),
                "score": task_scores.get("score"),
                "solvable": task_scores.get("solvable"),
                "resolution": resolution,
                "resolution_final": bool(submitted or resolution_event["final"]) if resolution_event else None,
                "seconds": seconds,
                "local_ms": local_ms,
                "usage": task_usage,
                "models": dict(Counter(row["model"] for row in task_calls if row.get("model") and row["calls"]))
                if events
                else None,
                "route": route["name"] if route else None,
                "routing": route,
                "initial_route": initial_route,
                "initial_step_limit": initial_attempt["step_limit"] if initial_attempt else None,
                "escalation_decision": decision,
                "attempts": attempts,
                "escalation": {
                    "reason": escalation["reason"],
                    "workspace": escalation["workspace"],
                    "before_usage": usage_total(
                        [row for row in task_calls if row["sequence"] < escalation["sequence"]],
                        reliable=contiguous, ledger=ledger,
                    ),
                    "after_usage": usage_total(
                        [row for row in task_calls if row["sequence"] > escalation["sequence"]],
                        reliable=contiguous and any(row["stage"] == "escalated" for row in attempts), ledger=ledger,
                    ),
                } if escalation else None,
                "plan": plan,
                "planning_decision": admission,
                "priority": plan["priority"] if plan else None,
                "estimated_cost": estimated_cost,
                "estimated_local_ms": forecast.get("estimated_local_ms"),
                "actual_cost": actual_cost,
                "cost_prediction_error": round(estimated_cost - actual_cost, 6)
                if estimated_cost is not None and actual_cost is not None else None,
                "tools": tool_stats(task_events) if task_events else None,
                "retransmitted_context_bytes": context["retransmitted_bytes"] if context else None,
                "retry_count": len(events_named(task_events, "task.resumed")) if task_events else None,
                "success_probability": forecast.get("success_probability"),
                "budget_after": budget_after,
                "budget_remaining": equivalent(
                    (budget_after or admission or {}).get("remaining_tenths")
                ),
                "stop_reason": stop_reason,
            }
        )
    if set(score_by_id) - {row["task_id"] for row in rows}:
        raise ValueError("Score contains tasks absent from the input inventory")
    return rows, published
