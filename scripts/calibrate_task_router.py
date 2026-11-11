"""Build model evidence from matched, independently scored control runs."""

import argparse
import json
from collections import defaultdict
from pathlib import Path
from statistics import mean


def read_run(directory):
    manifest = json.loads((directory / "run-manifest.json").read_text())
    runtime = manifest["runtime"]
    if runtime.get("routing") or manifest["launch"]["proxy_kind"] != "organizer":
        raise ValueError(f"Calibration needs a control run on the organizer proxy: {directory}")
    model = runtime["model"]
    rows = {}
    with (directory / "results-by-task.jsonl").open() as source:
        for line in source:
            row = json.loads(line)
            usage = row["usage"]
            if (
                type(row["passed"]) is not bool or not usage["usage_complete"]
                or row["status"] in {"not_started", "interrupted", "budget_exceeded", "proxy_error", "task_error"}
                or not usage["calls"] or set(row["models"]) != {model}
            ):
                raise ValueError(f"Incomplete or noncomparable calibration: {directory}/{row['task_id']}")
            rows[row["task_id"]] = row
    configuration = (manifest["input"]["content_sha256"], runtime["mode"], runtime.get("direct_publish"),
                     runtime.get("generation_limits"), runtime.get("max_steps"))
    return model, configuration, rows


def calibrate(initial_model, directories):
    runs = {}
    reference = None
    for directory in directories:
        model, configuration, rows = read_run(directory)
        if model in runs or (reference is not None and configuration != reference):
            raise ValueError("Use one run per model on matching prepared inputs and agent settings.")
        reference = configuration
        runs[model] = rows
    if len(runs) < 2 or initial_model not in runs:
        raise ValueError("Supply matched runs for the initial model and at least one alternative.")

    initial = runs[initial_model]
    if any(rows.keys() != initial.keys() for rows in runs.values()):
        raise ValueError("Use runs with the same independently checked tasks.")
    groups = defaultdict(list)
    for task_id, row in initial.items():
        groups[row["task_type"]].append(task_id)
    by_type = {}
    for kind, ids in sorted(groups.items()):
        failures = [task_id for task_id in ids if not initial[task_id]["passed"]]
        by_type[kind] = [
            {
                "model": model,
                "tasks": len(ids),
                "solved": sum(rows[task_id]["passed"] for task_id in ids),
                "mean_cost": mean(rows[task_id]["usage"]["output_equivalent"] for task_id in ids),
                "after_failure_tasks": len(failures),
                "after_failure_solved": sum(rows[task_id]["passed"] for task_id in failures),
            }
            for model, rows in sorted(runs.items())
        ]
    return {"schema_version": 1, "cheap_model": initial_model, "content_sha256": reference[0], "by_type": by_type}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initial-model", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("run_dirs", type=Path, nargs="+")
    args = parser.parse_args()
    try:
        profile = calibrate(args.initial_model, args.run_dirs)
        with args.output.open("x", encoding="utf-8") as file:
            json.dump(profile, file, indent=2)
            file.write("\n")
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
