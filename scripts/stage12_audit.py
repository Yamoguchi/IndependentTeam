"""Inventory independently scored planning trajectories without running the agent."""

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from contest_harness.provenance import runtime_hashes  # noqa: E402

FULL_ATTEMPTS = {"submitted", "no_answer", "generation_failed", "step_limit"}
FEATURES = {
    "instruction_chars", "files_seen", "file_bytes_seen", "file_types", "scan_limited",
    "operations", "complexity",
}
# Related tasks from the live pilots share a group even when their wording differs.
PILOT_FAMILIES = {
    "pilot-digit-math": "pilot/digit-enumeration",
    "pilot-smart-math": "pilot/digit-enumeration",
    "pilot-local-arithmetic": "pilot/explicit-arithmetic",
    "pilot-local-csv": "archive/csv-filter-and-sum",
    "pilot-local-lookup": "pilot/key-value-lookup",
    "range-repair": "pilot/interval-merge",
    "search-missing-archive": "pilot/required-file-missing",
    "vault-close": "pilot/required-file-missing",
    "revision-audit": "pilot/required-directory-wrong-type",
    "depot-route": "pilot/constrained-shortest-path",
    "relay-owner": "archive/search-two-hop",
    "published-index": "pilot/alternate-source",
    "spectrum-audit": "pilot/median-record",
    "rolling-margin": "pilot/csv-join-and-sum",
    "daily-peak": "pilot/group-and-median",
    "sealed-tally": "pilot/modular-recurrence",
    "alternating-cards": "pilot/constrained-permutations",
}


def load_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def families_for(archive):
    families = dict(PILOT_FAMILIES)
    for path in sorted(archive.glob("*/task.json")):
        task = json.loads(path.read_text())
        kind = task["task_type"]
        instruction = task["instruction"]
        if kind == "math":
            family = re.sub(r"\d+", "#", instruction).split(" starts ")[0].split(" has ")[0]
        elif kind == "search":
            family = "search-two-hop" if "headquartered" in instruction else "search-birth-year"
        elif kind == "coding":
            family = "single-function-python-repair"
        elif kind == "data_analysis":
            family = "csv-filter-and-sum"
        else:
            continue
        families[task["task_id"]] = f"archive/{family}"
    return families


def inventory(logs, historical):
    eligible = {}
    counts = Counter()
    for path in sorted(logs.rglob("run-manifest.json"), key=lambda p: (len(p.parts), p)):
        if path.relative_to(logs).parts[0] == "stage-12":
            continue
        manifest = json.loads(path.read_text())
        proxy = manifest.get("launch", {}).get("proxy_kind")
        counts[proxy or "unmarked"] += 1
        if path != historical and proxy not in {"organizer", "test-double"}:
            continue
        runtime = manifest.get("runtime") or {}
        journal = manifest.get("journal", {})
        scored = path.with_name("score.jsonl").is_file()
        results = path.with_name("results-by-task.jsonl").is_file()
        complete = journal.get("trace_sequence_contiguous") and (
            journal.get("usage_totals_match_finish") or (
                path == historical and journal.get("call_usage_matches_task_totals")
            )
        )
        if not (runtime.get("run_id") and scored and results and complete):
            counts[f"{proxy or 'historical'}_excluded"] += 1
            continue
        source = "historical" if path == historical else proxy
        counts[f"{source}_scored_reports"] += 1
        eligible.setdefault(runtime["run_id"], (path, manifest, source))
    return eligible, counts


def observations(path, manifest, source, families, current_hashes):
    runtime = manifest["runtime"]
    scores = {row.get("task_id"): row for row in load_jsonl(path.with_name("score.jsonl"))}
    results = load_jsonl(path.with_name("results-by-task.jsonl"))
    task_ids = {row["task_id"] for row in manifest["input"]["tasks"]}
    if len(results) != len(task_ids) or {row["task_id"] for row in results} != task_ids:
        raise ValueError(f"Inconsistent task inventory in {path.parent}")
    for row in results:
        task_id = row["task_id"]
        score = scores.get(task_id, {})
        passed = score.get("passed")
        if passed != row.get("passed"):
            raise ValueError(f"Inconsistent independent score for {task_id} in {path.parent}")
        route = row.get("initial_route") or row.get("routing") or {}
        usage = row["usage"]
        cost = usage.get("exact_tenths")
        if cost is None and usage.get("usage_complete") and usage.get("output_equivalent") is not None:
            cost = round(usage["output_equivalent"] * 10)
        plan = row.get("plan") or {}
        features = plan.get("features") or route.get("features") or {}
        forecast = plan.get("forecast") or {}
        initial_attempts = [a for a in row.get("attempts", []) if a.get("stage") == "initial"]
        yield {
            "source": source, "report": str(path.parent), "run_id": runtime["run_id"],
            "task_id": task_id, "family": families.get(task_id),
            "instruction_sha256": row.get("instruction_sha256"), "task_type": row["task_type"],
            "reward": row["reward"], "route": route.get("name") or row.get("route") or "BASELINE",
            "model": route.get("model", runtime.get("model")),
            "smart_model": (route.get("smart") or {}).get("model") if route.get("name") == "CASCADE" else None,
            "mode": runtime.get("mode", "baseline" if source == "historical" else None),
            "planner": runtime.get("planner"), "early_stop": runtime.get("early_stop", False),
            "settings": forecast.get("settings") or {
                "mode": runtime.get("mode"), "generation_limits": runtime.get("generation_limits"),
                "direct_publish": runtime.get("direct_publish", False),
                "step_limit": row.get("initial_step_limit") or runtime.get("max_steps"),
                "escalation_penalty": runtime.get("cascade_lambda") if route.get("name") == "CASCADE" else None,
            },
            "current_agent_hash": runtime.get("code_sha256") == current_hashes,
            "features": {name: features[name] for name in sorted(FEATURES & features.keys())},
            "estimated_tenths": forecast.get("estimated_tenths"),
            "first_reserve_tenths": forecast.get("first_reserve_tenths"),
            "cost_source": forecast.get("cost_source"), "cost_samples": forecast.get("cost_samples"),
            "success_probability": forecast.get("success_probability"),
            "success_samples": forecast.get("success_samples"),
            "exact_tenths": cost, "calls": usage.get("calls"), "local_ms": row.get("local_ms"),
            "started": row.get("started"), "status": row["status"],
            "resolution": (row.get("resolution") or {}).get("status"), "passed": passed,
            "solvable": score.get("solvable"), "score": score.get("score"),
            "initial_status": initial_attempts[0]["status"] if initial_attempts else None,
            "escalated": bool(row.get("escalation")),
        }


def coverage(rows):
    failed = [r for r in rows if r["passed"] is False]
    complete = [
        r for r in rows if type(r["passed"]) is bool and r["status"] in FULL_ATTEMPTS
        and r["started"] and r["exact_tenths"] is not None
    ]
    predictions = [r for r in complete if r["estimated_tenths"] is not None]
    positive = [r for r in predictions if r["exact_tenths"] > 0]
    return {
        "runs": len({r["run_id"] for r in rows}),
        "rows": len(rows),
        "tasks": len({r["task_id"] for r in rows}),
        "families": len({r["family"] for r in rows if r["family"] is not None}),
        "passed": sum(r["passed"] is True for r in rows),
        "failed": len(failed),
        "status": dict(Counter(r["status"] for r in rows)),
        "resolutions": dict(Counter(r["resolution"] or "unrecorded" for r in rows)),
        "complete": len(complete),
        "complete_tasks": len({r["task_id"] for r in complete}),
        "complete_families": len({r["family"] for r in complete if r["family"] is not None}),
        "complete_positive_cost_tasks": len({r["task_id"] for r in complete if r["exact_tenths"] > 0}),
        "complete_failures": sum(r["passed"] is False for r in complete),
        "complete_failed_tasks": len({r["task_id"] for r in complete if r["passed"] is False}),
        "complete_failed_families": len({
            r["family"] for r in complete if r["passed"] is False and r["family"] is not None
        }),
        "complete_passed_tasks": len({r["task_id"] for r in complete if r["passed"] is True}),
        "complete_attempts_with_features": sum(bool(r["features"]) for r in complete),
        "escalated": sum(r["escalated"] for r in rows),
        "escalated_tasks": len({r["task_id"] for r in rows if r["escalated"]}),
        "complete_attempts_with_recorded_cost_forecast": len(predictions),
        "complete_attempts_with_recorded_success_probability": sum(
            r["success_probability"] is not None for r in complete
        ),
        "positive_cost_with_forecast": len(positive),
        "descriptive_mae": mean(abs(r["estimated_tenths"] - r["exact_tenths"]) / 10
                                for r in predictions) if predictions else None,
        "descriptive_mape": mean(abs(r["estimated_tenths"] / r["exact_tenths"] - 1)
                                 for r in positive) * 100 if positive else None,
    }


def grouped(rows, key):
    groups = defaultdict(list)
    for row in rows:
        groups[key(row)].append(row)
    return {name: coverage(group) for name, group in sorted(groups.items())}


def planner_name(row):
    return "G" if row["early_stop"] else row["planner"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--logs", type=Path, default=Path("logs"))
    parser.add_argument("--archive-tree", type=Path, default=Path("task-dir"))
    parser.add_argument("--historical", type=Path, default=Path("stage-01/baseline-100/run-manifest.json"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    runs, manifests = inventory(args.logs, args.logs / args.historical)
    families = families_for(args.archive_tree)
    current_hashes = runtime_hashes()
    rows = [row for run in runs.values() for row in observations(*run, families, current_hashes)]
    live = [r for r in rows if r["source"] == "organizer"]
    planned = [r for r in live if r["planner"] in {"control", "F"}]
    summary = {
        "schema_version": 1,
        "manifests": dict(manifests),
        "sources": grouped(rows, lambda r: r["source"]),
        "all_live_including_historical": coverage([r for r in rows if r["source"] != "test-double"]),
        "live_current_agent_hash": coverage([r for r in live if r["current_agent_hash"]]),
        "live_unclassified_tasks": sorted({r["task_id"] for r in live if r["family"] is None}),
        "live_by_model": grouped(live, lambda r: r["model"] or "local"),
        "live_by_type": grouped(live, lambda r: r["task_type"]),
        "live_by_route": grouped(live, lambda r: r["route"]),
        "live_by_type_and_route": grouped(live, lambda r: f"{r['task_type']} / {r['route']}"),
        "live_by_settings": grouped(live, lambda r: json.dumps(
            [r["task_type"], r["route"], r["model"], r["smart_model"], r["early_stop"], r["settings"]],
            sort_keys=True,
        )),
        "live_planned_by_mode": grouped(planned, planner_name),
        "live_planned_by_stage": grouped(planned, lambda r: Path(r["report"]).relative_to(args.logs).parts[0]),
        "live_current_planned_by_mode": grouped(
            [r for r in planned if r["current_agent_hash"]], planner_name,
        ),
    }
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / "observations.jsonl").open("x") as stream:
        stream.writelines(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    with (args.output / "coverage.json").open("x") as stream:
        json.dump(summary, stream, indent=2, sort_keys=True)
        stream.write("\n")


if __name__ == "__main__":
    main()
