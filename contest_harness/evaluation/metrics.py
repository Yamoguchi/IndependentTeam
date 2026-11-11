"""Aggregate benchmark task, budget, route and timing metrics."""

from collections import Counter, defaultdict
from math import ceil, log, log1p
from statistics import mean, median

from contest_harness.planning.feasibility import REFUSALS
from contest_harness.planning.forecast import FULL_ATTEMPTS, MIN_SUCCESS_SAMPLES
from contest_harness.telemetry.usage import Usage

USAGE_FIELDS = (
    "calls",
    "measured_calls",
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_tokens",
)
LEDGER_FIELDS = ("held_tenths", "estimate_overruns", "estimate_overrun_tenths", "denied_calls", "not_sent_calls")


def equivalent(tenths):
    return tenths / 10 if tenths is not None else None


def ratio(numerator, denominator):
    return round(numerator / denominator, 6) if numerator is not None and denominator else None


def sum_known(values):
    return sum(values) if None not in values else None


def usage_total(rows, *, reliable=True, ledger=False):
    result = {field: sum_known([row.get(field) for row in rows]) for field in USAGE_FIELDS}
    result.update(
        {field: sum_known([row.get(field) for row in rows]) if rows or ledger else None
         for field in LEDGER_FIELDS}
    )
    measured = result["measured_calls"]
    calls = result["calls"]
    tenths = None
    if result["input_tokens"] is not None and result["output_tokens"] is not None:
        tenths = result["input_tokens"] + 10 * result["output_tokens"]
    complete = reliable and calls is not None and calls == measured
    return {
        **result,
        "unknown_usage_calls": calls - measured if calls is not None and measured is not None else None,
        "usage_coverage": ratio(measured, calls),
        "usage_complete": complete,
        "usage_status": "exact" if complete else "unknown",
        "measured_tenths": tenths,
        "exact_tenths": tenths if complete else None,
        "measured_output_equivalent": tenths / 10 if tenths is not None else None,
        "output_equivalent": tenths / 10 if complete and tenths is not None else None,
    }


def distribution(values, percentile=90):
    values = sorted(value for value in values if value is not None)
    return {
        "samples": len(values),
        "sum": round(sum(values), 6) if values else None,
        "mean": round(mean(values), 6) if values else None,
        "median": round(median(values), 6) if values else None,
        f"p{percentile}": values[ceil(len(values) * percentile / 100) - 1] if values else None,
        "max": max(values, default=None),
    }


def events_named(events, name):
    return [event for event in events if event["event"] == name]


def token_rows(events):
    calls = {}
    for event in events:
        if event["event"] not in {"llm.started", "llm.denied", "llm.finished"}:
            continue
        key = (event["run_id"], event["call_id"])
        if key not in calls or event["event"] == "llm.finished":
            calls[key] = event
    rows = []
    for event in calls.values():
        row = {
            key: event.get(key)
            for key in (
                "run_id", "call_id", "sequence", "task_index", "task_id", "session_id",
                "model", "duration_ms", "budget_wait_ms", "proxy_usage",
                "finish_reason", "wire_finish_reason", "generation_profile", "generation_recovery",
                "generation_rejection",
            )
        }
        usage = event.get("usage")
        estimate = event.get("estimate")
        denied = event["event"] == "llm.denied"
        if usage is None and estimate:
            usage = Usage(denied_calls=1).report() if denied else Usage(
                calls=1, held_tenths=estimate["tenths"]
            ).report()
        rows.append(
            {
                **row,
                "event": "call",
                "status": event.get("status", "budget_denied" if denied else "unfinished"),
                "estimate": estimate,
                **(
                    usage
                    or {
                        "calls": 1,
                        "measured_calls": 0,
                        "input_tokens": 0,
                        "cached_input_tokens": 0,
                        "output_tokens": 0,
                        "reasoning_tokens": 0,
                        "output_equivalent": None,
                        "usage_complete": False,
                    }
                ),
            }
        )
        if not rows[-1]["usage_complete"]:
            rows[-1]["output_equivalent"] = None
        rows[-1]["usage_status"] = (
            "unknown" if not rows[-1]["usage_complete"] else "exact" if rows[-1]["calls"] else "not_sent"
        )
    return rows


def tool_stats(events):
    tools = defaultdict(list)
    for event in events:
        if event["event"] in {"tool.started", "tool.finished", "tool.unavailable"}:
            tools[event["tool"]].append(event)
    result = {}
    for name, rows in sorted(tools.items()):
        started = events_named(rows, "tool.started")
        finished = events_named(rows, "tool.finished")
        unavailable = events_named(rows, "tool.unavailable")
        signatures = [
            (row.get("task_index"), row["arguments"]["sha256"]) for row in started if row.get("arguments")
        ]
        statuses = Counter(row["status"] for row in finished)
        started_ids = {(row.get("call_id"), row.get("tool_call_id")) for row in started}
        missing = sum((row.get("call_id"), row.get("tool_call_id")) not in started_ids for row in unavailable)
        total = len(started) + missing
        has_statuses = unavailable or any(row.get("status_source") == "harness" for row in finished)
        invalid = statuses["invalid"] + missing if has_statuses else None
        result[name] = {
            "calls": total,
            "finished": len(finished),
            "ok": statuses["ok"],
            "errors": statuses["error"],
            "error_rate": ratio(statuses["error"], total),
            "invalid_actions": invalid,
            "invalid_action_rate": ratio(invalid, total),
            "unavailable_reasons": dict(Counter(row["reason"] for row in unavailable)) if has_statuses else None,
            "repeated_arguments": len(signatures) - len(set(signatures)),
            "duration_ms": distribution([row.get("duration_ms") for row in finished]),
            "output_bytes": distribution([(row.get("result") or {}).get("utf8_bytes") for row in finished]),
        }
    return result


def context_stats(events, calls):
    requests = events_named(events, "llm.request")
    first, repeated = [], []
    seen = set()
    for row in calls:
        index = row.get("task_index")
        if index is None or not row["calls"]:
            continue
        bucket = repeated if index in seen else first
        seen.add(index)
        prompt = (row.get("proxy_usage") or {}).get("input_tokens")
        if prompt is not None or row.get("measured_calls") == 1:
            bucket.append(prompt if prompt is not None else row["input_tokens"])
    retransmitted = {"messages": 0, "tool_messages": 0, "tool_schemas": 0}
    messages_by_task = defaultdict(set)
    schemas_by_task = defaultdict(set)
    for request in requests:
        task = request.get("task_index")
        messages = request["context"]["messages"]
        schemas = request["tools"]["items"]
        for row in messages:
            shape = row["message"]
            if shape["sha256"] in messages_by_task[task]:
                retransmitted["messages"] += shape["utf8_bytes"]
                if row["role"] == "tool":
                    retransmitted["tool_messages"] += shape["utf8_bytes"]
        for row in schemas:
            if row["schema"]["sha256"] in schemas_by_task[task]:
                retransmitted["tool_schemas"] += row["schema"]["utf8_bytes"]
        messages_by_task[task].update(row["message"]["sha256"] for row in messages)
        schemas_by_task[task].update(row["schema"]["sha256"] for row in schemas)
    return {
        "first_input_tokens": distribution(first),
        "repeat_input_tokens": distribution(repeated),
        "repeat_input_fraction": ratio(sum(repeated), sum(first) + sum(repeated)),
        "input_tokens_change": distribution(
            [row.get("input_tokens_change") for row in events_named(events, "llm.finished")]
        ),
        "message_bytes": distribution([row["context"]["serialized"]["utf8_bytes"] for row in requests]),
        "tool_schema_bytes": distribution([row["tools"]["serialized"]["utf8_bytes"] for row in requests]),
        "response_content_bytes": distribution(
            [
                (row.get("response") or {}).get("content", {}).get("utf8_bytes")
                for row in events_named(events, "llm.finished")
            ]
        ),
        "retransmitted_bytes": retransmitted if requests else None,
        "window_selection": {
            "calls": len(selection),
            "compacted": sum(row["compacted"] for row in selection),
            "dropped_exchanges": sum(row["dropped_exchanges"] for row in selection),
            "stale_exchanges": sum(row["stale_exchanges"] for row in selection),
        } if (selection := events_named(events, "context.selected")) else None,
    }


def route_stats(tasks, *, scored):
    routed = [row for row in tasks if row["route"] is not None]
    if not routed:
        return None
    escalated = [row for row in routed if row["escalation"] is not None]
    checked_escalations = [row for row in escalated if row["passed"] is not None]
    local = [row for row in routed if row["publication_source"] == "local"]
    correct = sum(row["passed"] is True for row in tasks)
    initial_correct = sum(row["passed"] is True for row in routed
                          if row["publication_source"] != "local" and row["escalation"] is None)
    by_route = {}
    for name in sorted({row["route"] for row in routed}):
        group = [row for row in routed if row["route"] == name]
        by_route[name] = {
            "tasks": len(group), "correct": sum(row["passed"] is True for row in group) if scored else None,
            "score": sum(row["score"] for row in group if row["score"] is not None) if scored else None,
            "output_equivalent": sum_known([row["usage"]["output_equivalent"] for row in group]),
        }
    return {
        "by_route": by_route,
        "initial_models": dict(Counter(row["routing"]["model"] for row in routed if row["routing"]["model"])),
        "escalated_models": dict(Counter(row["escalation_decision"]["to_model"] for row in escalated)),
        "local_publications": len(local),
        "local_solution_fraction": ratio(sum(row["passed"] is True for row in local), correct) if scored else None,
        "local_fraction_of_tasks": ratio(sum(row["passed"] is True for row in local), len(tasks)) if scored else None,
        "initial_model_solution_fraction": ratio(initial_correct, correct) if scored else None,
        "escalations": len(escalated),
        "escalation_fraction": ratio(len(escalated), len(routed)),
        "escalation_success_fraction": ratio(
            sum(row["passed"] is True for row in checked_escalations), len(checked_escalations),
        )
        if scored else None,
        "escalations_without_gain": sum(not row["score"] or row["score"] <= 0 for row in checked_escalations)
        if scored else None,
        "escalations_unchecked": len(escalated) - len(checked_escalations),
        "escalation_points": sum(row["score"] for row in escalated if row["score"] is not None)
        if scored else None,
        "before_escalation": usage_total(
            [row["escalation"]["before_usage"] for row in escalated],
            reliable=all(row["escalation"]["before_usage"]["usage_complete"] for row in escalated), ledger=True,
        ),
        "after_escalation": usage_total(
            [row["escalation"]["after_usage"] for row in escalated],
            reliable=all(row["escalation"]["after_usage"]["usage_complete"] for row in escalated), ledger=True,
        ),
        "decisions": dict(Counter(row["escalation_decision"]["reason"] for row in routed if row["escalation_decision"])),
    }


def summarize(tasks, usage, calls, events, *, scored, trace_available):
    correct = sum(row["passed"] is True for row in tasks) if scored else None
    score = sum(row["score"] for row in tasks if row["score"] is not None) if scored else None
    max_score = sum_known([row.get("reward") for row in tasks]) if tasks else None
    cost = usage["output_equivalent"]
    per_task = [row["usage"]["output_equivalent"] for row in tasks if row["started"]]
    tool_starts = events_named(events, "tool.started")
    tools = tool_stats(events) if trace_available else None
    tool_finishes = events_named(events, "tool.finished")
    compacted = events_named(events, "tool.output_compacted")
    validated = events_named(events, "answer.validated")
    publications = events_named(events, "answer.saved")
    model_time = distribution([row.get("duration_ms") for row in calls if row["calls"]], 95)
    tool_time = distribution([row.get("duration_ms") for row in tool_finishes])
    observed_time = (model_time["sum"] or 0) + (tool_time["sum"] or 0)
    return {
        "tasks": len(tasks),
        "attempted": sum(row["started"] for row in tasks),
        "submitted": sum(row["submitted"] for row in tasks),
        "locally_checked": sum(row["passed"] is not None for row in tasks) if scored else None,
        "correct": correct,
        "success_rate": ratio(correct, len(tasks)),
        "score": score,
        "max_score": max_score,
        "official_score": None,
        **usage,
        "task_output_equivalent": distribution(per_task),
        "output_equivalent_per_successful_task": ratio(cost, correct),
        "output_equivalent_per_point": ratio(cost, score),
        "points_per_1000_output_equivalent": ratio(score * 1000 if score is not None else None, cost),
        "calls_per_attempted_task": ratio(
            sum_known([row["usage"]["calls"] for row in tasks]), sum(row["started"] for row in tasks)
        ),
        "models": dict(Counter(row["model"] for row in calls if row.get("model") and row["calls"]))
        if trace_available
        else None,
        "routes": route_stats(tasks, scored=scored),
        "context": context_stats(events, calls) if trace_available else None,
        "tools": tools,
        "answer_validation": {
            "attempts": len(validated),
            "rejected": sum(not event["accepted"] for event in validated),
            "format": dict(Counter(event["format"]["status"] for event in validated)),
            "semantic": dict(Counter(event["semantic"]["status"] for event in validated)),
            "publication_sources": dict(Counter(event["source"] for event in publications if "source" in event)),
        } if validated else None,
        "tool_calls": sum(row["calls"] for row in tools.values()) if tools is not None else None,
        "tool_output_bytes": sum((row.get("result") or {}).get("utf8_bytes", 0) for row in tool_finishes)
        if trace_available else None,
        "compacted_tool_outputs": len(compacted) if trace_available else None,
        "repeated_tool_outputs": sum(row["repeated"] for row in compacted) if trace_available else None,
        "detail_requests": sum(row.get("tool") == "output_details" for row in tool_starts)
        if trace_available else None,
        "generation_truncations": sum(
            (row.get("wire_finish_reason") or row.get("finish_reason")) == "length" for row in calls
        ) if trace_available else None,
        "generation_recoveries": sum(bool(row.get("generation_recovery")) and bool(row["calls"]) for row in calls)
        if trace_available else None,
        "generation_recovery_denials": sum(
            bool(row.get("generation_recovery")) and row["status"] == "budget_denied" for row in calls
        ) if trace_available else None,
        "generation_rejections": dict(Counter(row["generation_rejection"] for row in calls if row.get("generation_rejection")))
        if trace_available else None,
        "time": {
            "task_seconds": distribution([row["seconds"] for row in tasks]),
            "model_ms": model_time,
            "tool_ms": tool_time,
            "model_fraction": ratio(model_time["sum"], observed_time),
            "tool_fraction": ratio(tool_time["sum"], observed_time),
            "budget_wait_ms": distribution([row.get("budget_wait_ms") for row in calls]),
        },
    }


def comparison_conditions(manifest):
    launch = manifest.get("launch", {})
    runtime = manifest.get("runtime") or {}
    return {
        "model": runtime.get("model") or launch.get("model"),
        "proxy_kind": launch.get("proxy_kind"),
        "backend": launch.get("backend"),
        "limit": launch.get("local_limit_tenths"),
    }


def comparison(summary, manifest):
    baseline = manifest.get("comparison_reference")
    if baseline is None:
        return {
            "baseline": None,
            "same_prepared_content": None,
            "same_conditions": None,
            "deltas": {},
            "context_input_reduction_fraction": None,
        }
    reference = baseline["summary"]
    old_content = baseline["content_sha256"]
    same = old_content == manifest["input"].get("content_sha256") if old_content else None
    old_conditions = baseline.get("conditions", {})
    new_conditions = comparison_conditions(manifest)
    same_conditions = (
        old_conditions == new_conditions
        if all(conditions.get("model") and conditions.get("proxy_kind") for conditions in (old_conditions, new_conditions))
        else None
    )
    fields = (
        "score",
        "correct",
        "success_rate",
        *USAGE_FIELDS,
        "output_equivalent",
        "seconds",
        "output_equivalent_per_point",
        "points_per_1000_output_equivalent",
        "tool_errors",
        "tool_calls",
        "tool_output_bytes",
        "detail_requests",
        "generation_truncations",
        "generation_recoveries",
        "escalations",
        "escalations_without_gain",
    )
    deltas = {}
    for field in fields:
        old, new = reference.get(field), summary.get(field)
        change = round(new - old, 6) if new is not None and old is not None else None
        percent = ratio(change * 100 if change is not None else None, old)
        deltas[field] = {"baseline": old, "current": new, "absolute": change, "percent": percent}
    input_change = deltas["input_tokens"]["percent"]
    return {
        "baseline": baseline["path"],
        "same_prepared_content": same,
        "same_conditions": same_conditions,
        "deltas": deltas,
        "context_input_reduction_fraction": round(-input_change / 100, 6)
        if input_change is not None
        and same is True
        and summary["usage_complete"]
        and reference.get("usage_complete")
        else None,
    }


def prediction_errors(pairs):
    positive = [(predicted, actual) for predicted, actual in pairs if actual > 0]
    return {
        "samples": len(pairs),
        "mae": round(mean(abs(predicted - actual) for predicted, actual in pairs), 6) if pairs else None,
        "mape_samples": len(positive),
        "mape_percent": round(100 * mean(abs(predicted - actual) / actual for predicted, actual in positive), 6)
        if positive else None,
    }


def probability_metrics(labeled):
    result = {"probability_samples": len(labeled), "brier_score": None, "log_loss": None, "calibration": None}
    if len(labeled) < MIN_SUCCESS_SAMPLES:
        return result
    bins = defaultdict(list)
    for probability, outcome in labeled:
        bins[min(int(probability * 10), 9)].append((probability, outcome))
    epsilon = 1e-15
    bounded = [(min(1 - epsilon, max(epsilon, p)), y) for p, y in labeled]
    return {
        **result,
        "brier_score": round(mean((p - y) ** 2 for p, y in labeled), 6),
        "log_loss": round(-mean(log(p) if y else log1p(-p) for p, y in bounded), 6),
        "calibration": [
            {"range": [index / 10, (index + 1) / 10], "samples": len(group),
             "mean_prediction": round(mean(p for p, _ in group), 6),
             "observed_rate": round(mean(y for _, y in group), 6)}
            for index, group in sorted(bins.items())
        ],
    }


def planning_stats(rows, ready):
    if ready is None:
        return None
    observed = [
        (row["estimated_cost"], row["actual_cost"])
        for row in rows if row["estimated_cost"] is not None and row["actual_cost"] is not None
    ]
    full = [
        (row["estimated_cost"], row["actual_cost"])
        for row in rows if row["status"] in FULL_ATTEMPTS
        and row["estimated_cost"] is not None and row["actual_cost"] is not None
    ]
    overruns = [actual - predicted for predicted, actual in observed if actual > predicted]
    skipped = Counter(row["stop_reason"] for row in rows if row["stop_reason"])
    labeled = [
        (row["success_probability"], row["passed"])
        for row in rows if row["status"] in FULL_ATTEMPTS and row["success_probability"] is not None
        and type(row["passed"]) is bool
    ]
    return {
        "mode": ready["planner"],
        "preflight_ms": ready["duration_ms"],
        "input_order": ready["input_order"],
        "planned_order": ready["planned_order"],
        "skipped": dict(skipped),
        "planning_errors": skipped["preflight_error"] + sum(
            bool(row["planning_decision"] and row["planning_decision"]["admitted"] and row["usage"]["denied_calls"])
            for row in rows
        ),
        "admitted_with_budget_denial": sum(
            bool(row["planning_decision"] and row["planning_decision"]["admitted"] and row["usage"]["denied_calls"])
            for row in rows
        ),
        "planned_route_changed": sum(
            bool(row["started"] and row["initial_route"] and row["plan"]
                 and (row["initial_route"]["name"], row["initial_route"]["model"])
                 != (row["plan"]["route"], row["plan"]["model"]))
            for row in rows
        ),
        "known_costs": prediction_errors(observed),
        "full_costs": prediction_errors(full),
        # Kept for consumers of existing experiment artifacts.
        "known_positive_costs": prediction_errors([(p, a) for p, a in observed if a > 0]),
        "full_positive_costs": prediction_errors([(p, a) for p, a in full if a > 0]),
        "cost_overruns": len(overruns),
        "cost_overrun_total": round(sum(overruns), 6),
        **probability_metrics(labeled),
    }


def early_stop_stats(rows, events):
    resolved = [row for row in rows if row["resolution"]]
    if not resolved:
        return None
    counts = Counter(row["resolution"]["status"] for row in resolved)
    refused = [row for row in resolved if row["resolution"]["status"] in REFUSALS]
    unresolved = [row for row in resolved if row["resolution"]["status"] != "DONE"]
    labeled_refusals = sum(type(row["solvable"]) is bool for row in refused)
    errors = sum(row["solvable"] is True for row in refused)
    has_labels = any(type(row["solvable"]) is bool for row in rows)

    def spend(group):
        usages = [row["usage"] for row in group]
        return {
            "exact_tenths": sum(row["exact_tenths"] for row in usages)
            if all(row["exact_tenths"] is not None for row in usages) else None,
            "measured_tenths": sum(row["measured_tenths"] for row in usages),
            "held_tenths": sum(row["held_tenths"] or 0 for row in usages),
        }

    return {
        "statuses": dict(counts),
        "reasons": dict(Counter(row["resolution"]["reason"] for row in resolved)),
        "refusals": len(refused),
        "labeled_refusals": labeled_refusals,
        "false_refusals": errors if has_labels and labeled_refusals == len(refused) else None,
        "false_refusal_fraction": ratio(errors, len(refused)) if labeled_refusals == len(refused) else None,
        "preflight_checks": len(events_named(events, "recognition.checked")),
        "recognition_ms": round(sum(row["duration_ms"] for row in events_named(events, "recognition.checked")), 3),
        "until_refusal": spend(refused),
        "until_non_completion": spend(unresolved),
        "known_unsolvable_spend": spend([row for row in rows if row["solvable"] is False]) if has_labels else None,
        "retries": len(events_named(events, "task.resumed")),
        "retry_decisions": dict(Counter(row["reason"] for row in events_named(events, "recognition.retry_decision"))),
    }
