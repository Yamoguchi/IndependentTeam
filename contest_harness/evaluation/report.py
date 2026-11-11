"""Assemble benchmark artifacts from trace events and scoring results."""

import json

from contest_harness.evaluation.artifacts import (
    SCHEMA_VERSION,
    read_jsonl,
    write_json,
    write_jsonl,
    write_text,
)
from contest_harness.evaluation.markdown import NA, markdown
from contest_harness.evaluation.metrics import (
    LEDGER_FIELDS,
    USAGE_FIELDS,
    comparison,
    comparison_conditions,
    early_stop_stats,
    events_named,
    planning_stats,
    ratio,
    summarize,
    token_rows,
    usage_total,
)
from contest_harness.evaluation.task_results import task_results


def generate(source, output, manifest, baseline=None):
    legacy = manifest["tokens_format"] == "task_totals"
    trace_path = source / "journal" / "trace.jsonl"
    if not trace_path.is_file():
        trace_path = source / (
            "trace/trace.jsonl" if legacy and (source / "trace/trace.jsonl").is_file() else "trace.jsonl"
        )
    stdout_path = source / ("tokens.jsonl" if legacy else "agent-stdout.jsonl")
    events, trace_tail = read_jsonl(trace_path)
    stdout, stdout_tail = read_jsonl(stdout_path)
    scores, score_tail = read_jsonl(source / "score.jsonl")
    score_by_id = {row["task_id"]: row for row in scores if "task_id" in row}
    if len(score_by_id) != sum("task_id" in row for row in scores):
        raise ValueError("Score contains duplicate task IDs")
    score_summary = next((row for row in scores if "max_score" in row), None)
    calls = token_rows(events)
    journal_usage = events_named(stdout, "task")
    original_totals = next(iter(events_named(stdout, "summary")), None)
    finishes = events_named(events, "run.finished")
    starts = events_named(events, "run.started")
    ledger = bool(starts and starts[0].get("reservation_policy"))
    usage_fields = (
        (*USAGE_FIELDS, *LEDGER_FIELDS, "measured_tenths", "exact_tenths") if ledger else USAGE_FIELDS
    )
    runtime_code = starts[0].get("code_sha256") if starts else None
    code = manifest.get("code", {})
    recorded_runtime = code.get("image_runtime_sha256")
    if (
        legacy
        and runtime_code is not None
        and recorded_runtime is not None
        and runtime_code != recorded_runtime
    ):
        raise ValueError("Historical trace runtime SHA-256 differs from the recorded image")
    contiguous = all(row.get("sequence") == n for n, row in enumerate(events, 1))
    observed = usage_total(journal_usage if legacy else calls, ledger=ledger)
    call_observed = usage_total(calls)
    trace_totals_match = (
        all(observed[key] == original_totals.get(key) for key in usage_fields) if original_totals else None
    )
    reliable = (
        bool(original_totals and trace_totals_match)
        if legacy
        else (
            bool(finishes and contiguous and not trace_tail)
            and all(observed[key] == finishes[-1].get(key) for key in usage_fields)
            and trace_totals_match is not False
        )
    )
    rows, published = task_results(
        source, manifest, events, calls, journal_usage, score_by_id,
        legacy=legacy, ledger=ledger, reliable=reliable, contiguous=contiguous,
    )
    ready = next(iter(events_named(events, "planning.ready")), None)
    usage = usage_total(journal_usage if legacy else calls, reliable=reliable, ledger=ledger)
    metrics = summarize(rows, usage, calls, events, scored=bool(scores), trace_available=bool(events))
    if score_summary and (
        metrics["correct"] != score_summary["correct"]
        or metrics["score"] != score_summary["score"]
        or metrics["max_score"] != score_summary["max_score"]
        or metrics["tasks"] != score_summary["tasks"]
        or metrics["locally_checked"] != metrics["tasks"]
    ):
        raise ValueError("Score totals differ from the per-task scores or the input inventory")
    launch = manifest.get("launch", {})
    budget = launch.get("proxy_budget_output_equivalent")
    local_limit = starts[0].get("local_limit_tenths") if ledger else launch.get("local_limit_tenths")
    held = usage["held_tenths"]
    occupied = usage["measured_tenths"] + held if held is not None and contiguous else None
    remaining = local_limit - occupied if local_limit is not None and occupied is not None else None
    recovered_reserve = sum(row["held_tenths"] for row in calls if row["status"] == "unfinished") if ledger else None
    runtime = finishes[-1] if finishes else (original_totals or {})
    metrics.update(
        seconds=runtime.get("seconds"),
        launcher_wall_seconds=launch.get("wall_seconds"),
        sessions=runtime.get("sessions", launch.get("sessions")),
        peak_active_sessions=runtime.get("peak_active_sessions"),
        observed_trace_seconds=round(events[-1]["elapsed_ms"] / 1000, 3) if events else None,
        planning=planning_stats(rows, ready),
        early_stop=early_stop_stats(rows, events),
        budget={
            "limit": budget,
            "used_fraction": ratio(usage["output_equivalent"], budget),
            "remaining": round(budget - usage["output_equivalent"], 6)
            if budget is not None and usage["output_equivalent"] is not None
            else None,
            "reservation": held / 10 if held is not None else None,
            "recovered_in_flight_tenths": recovered_reserve,
            "local_limit_tenths": local_limit,
            "occupied_tenths": occupied,
            "remaining_tenths": remaining,
            "local_overrun_tenths": max(0, -remaining) if remaining is not None else None,
            "local_occupied_fraction": ratio(occupied, local_limit),
        },
        execution_order=[
            row["task_id"]
            for row in sorted(
                (row for row in rows if row["started"]),
                key=lambda row: row["task_index"] or row["input_index"],
            )
        ],
        unavailable=NA,
    )
    grouped = {}
    for kind in sorted({row["task_type"] for row in rows}):
        group = [row for row in rows if row["task_type"] == kind]
        indices = {row["task_index"] for row in group if row["task_index"] is not None}
        group_usage = usage_total(
            [row["usage"] for row in group], reliable=all(row["usage"]["usage_complete"] for row in group)
        )
        grouped[kind] = summarize(
            group,
            group_usage,
            [row for row in calls if row["task_index"] in indices],
            [row for row in events if row.get("task_index") in indices],
            scored=bool(scores),
            trace_available=bool(events),
        )
    fields = (
        "tasks",
        "attempted",
        "submitted",
        "locally_checked",
        "correct",
        "success_rate",
        "score",
        "max_score",
        "official_score",
        *USAGE_FIELDS,
        *LEDGER_FIELDS,
        "unknown_usage_calls",
        "usage_coverage",
        "usage_complete",
        "usage_status",
        "exact_tenths",
        "measured_tenths",
        "output_equivalent",
        "measured_output_equivalent",
        "seconds",
        "launcher_wall_seconds",
        "output_equivalent_per_point",
        "points_per_1000_output_equivalent",
        "tool_calls",
        "tool_output_bytes",
        "compacted_tool_outputs",
        "repeated_tool_outputs",
        "detail_requests",
        "generation_truncations",
        "generation_recoveries",
        "generation_recovery_denials",
    )
    summary = {key: metrics[key] for key in fields}
    status = runtime.get("status")
    if launch.get("status") in {"timeout", "launch_error"}:
        status = launch["status"]
    if status is None and launch.get("status") == "running":
        status = "interrupted"
    summary.update(
        schema_version=SCHEMA_VERSION,
        status=status or launch.get("status") or "unknown",
        tool_errors=sum(row["errors"] for row in metrics["tools"].values())
        if metrics["tools"] is not None
        else None,
        escalations=metrics["routes"]["escalations"] if metrics["routes"] else None,
        escalations_without_gain=metrics["routes"]["escalations_without_gain"] if metrics["routes"] else None,
        models=metrics["models"],
    )
    if baseline is not None:
        reference = json.loads((baseline / "run-manifest.json").read_text())
        manifest["comparison_reference"] = {
            "path": str(baseline),
            "summary": json.loads((baseline / "summary.json").read_text()),
            "content_sha256": reference["input"].get("content_sha256"),
            "conditions": comparison_conditions(reference),
        }
    comparison_manifest = {**manifest, "runtime": starts[0] if starts else None}
    comparison_data = comparison(summary, comparison_manifest)
    if previous := manifest.get("previous_stage_reference"):
        comparison_data["previous_stage"] = comparison(
            summary, {**comparison_manifest, "comparison_reference": previous},
        )
    previous_journal = manifest.get("journal", {})
    manifest = {
        **manifest,
        "runtime": starts[0] if starts else None,
        "runtime_code_matches_recorded_files": all(
            code.get("files_sha256", {}).get(name) == digest for name, digest in runtime_code.items()
        )
        if runtime_code
        else None,
        "journal": {
            "trace_events": len(events),
            "trace_run_id": starts[0]["run_id"] if starts else None,
            "trace_sequence_contiguous": contiguous if events else None,
            "finished_event": bool(finishes),
            "trace_tail_discarded": trace_tail or previous_journal.get("trace_tail_discarded", False),
            "stdout_tail_discarded": stdout_tail or previous_journal.get("stdout_tail_discarded", False),
            "score_tail_discarded": score_tail or previous_journal.get("score_tail_discarded", False),
            "usage_totals_match_stdout": trace_totals_match,
            "usage_totals_match_finish": (
                all(observed[key] == finishes[-1].get(key) for key in usage_fields) if finishes else None
            ),
            "published_answer_directories": sorted(published),
            "call_usage_matches_task_totals": (
                all(call_observed[key] == observed[key] for key in USAGE_FIELDS) if legacy and calls else None
            ),
        },
    }
    write_json(output / "run-manifest.json", manifest)
    write_json(output / "summary.json", summary)
    write_json(output / "metrics.json", metrics)
    write_json(output / "results-by-type.json", grouped)
    write_jsonl(output / "results-by-task.jsonl", rows)
    write_jsonl(output / "tokens.jsonl", stdout if legacy else calls)
    write_jsonl(output / "trace.jsonl", events)
    if scores and output != source:
        write_jsonl(output / "score.jsonl", scores)
    write_json(output / "comparison.json", comparison_data)
    write_text(output / "report.md", markdown(summary, metrics, grouped, comparison_data, manifest, rows))
    return summary
