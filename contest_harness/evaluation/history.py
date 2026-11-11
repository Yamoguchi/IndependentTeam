"""Read historical runs and export observations for cost forecasts."""

import json
import re
from shutil import copyfile

from contest_harness.evaluation.artifacts import SCHEMA_VERSION, read_jsonl, write_jsonl
from contest_harness.evaluation.inputs import fingerprint, historical_input
from contest_harness.evaluation.manifest import code_metadata, git, load_manifest
from contest_harness.evaluation.report import generate
from contest_harness.json_codec import canonical_json
from contest_harness.planning.forecast import FULL_ATTEMPTS, settings_for
from contest_harness.planning.forecast import SCHEMA_VERSION as PLANNING_SCHEMA_VERSION


def historical_manifest(source, legacy):
    sums = source / "SHA256SUMS"
    verified = 0
    if sums.is_file():
        for line in sums.read_text().splitlines():
            digest, name = line.split("  ", 1)
            file = (source / name).resolve()
            if not file.is_relative_to(source) or fingerprint(file) != digest:
                raise ValueError(f"Historical SHA256SUMS mismatch: {name}")
            verified += 1
    harness = source / "harness"
    files = {path.name: fingerprint(path) for path in sorted(harness.iterdir()) if path.is_file()}
    runtime = json.loads((source / "image-runtime-sha256.json").read_text())
    if any(files.get(name) != digest for name, digest in runtime.items()):
        raise ValueError("Historical runtime code differs from the saved harness")
    inputs, rows = historical_input(source, legacy)
    # Verify the reconstructed original metadata against the recorded mounted tree.
    for row in rows:
        if row["path"].count("/") == 1 and row["path"].endswith("/task.json"):
            if fingerprint(source / "tasks" / row["path"]) != row["sha256"]:
                raise ValueError(f"Historical mounted metadata differs: {row['path']}")
    deps = fingerprint(source / "runtime-dependencies.txt")
    if deps != legacy.get("verification", {}).get("runtime_dependencies_sha256"):
        raise ValueError("Historical runtime dependency fingerprint differs")
    historical_head = legacy.get("git_head")
    head_files = (
        {name: git("rev-parse", "--verify", f"{historical_head}:{name}") for name in files}
        if historical_head and re.fullmatch(r"[0-9a-f]{40}", historical_head)
        else {}
    )
    # Archived bytes, including untracked run_trace.py, identify the measured code.
    matches_head = (
        {name: head_files.get(name) == git("hash-object", str(harness / name)) for name in files}
        if head_files
        else {}
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "tokens_format": "task_totals",
        "created_at": legacy.get("created_at"),
        "input": inputs,
        "code": {
            "git_head": historical_head,
            "git_dirty": True if matches_head and not all(matches_head.values()) else None,
            "matches_recorded_git_head": matches_head,
            "files_sha256": files,
            "image_runtime_sha256": runtime,
            "runtime_dependencies_sha256": deps,
        },
        "launch": {
            "status": legacy["status"],
            "mode": "baseline",
            "backend": "docker",
            "image_id": legacy.get("image_id"),
            "model": legacy.get("model"),
            "cpu_limit": legacy.get("cpu_limit"),
            "memory_limit": legacy.get("memory_limit"),
            "time_limit_seconds": legacy.get("container_time_limit_seconds"),
            "started_at": legacy.get("started_at"),
            "finished_at": legacy.get("finished_at"),
            "exit_code": legacy.get("agent_exit_code"),
            "scorer_exit_code": legacy.get("scorer_exit_code"),
            "proxy_budget_output_equivalent": None,
            "sessions": 1,
        },
        "historical_checksums_verified": verified,
    }, rows


def report(args):
    source, output = args.source_run, args.run_dir
    if output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("The new report directory must be separate from its source")
    if not (source / "run-manifest.json").is_file():
        manifest, rows = unversioned_manifest(source), None
    elif "tokens_format" not in (manifest := load_manifest(source)):
        manifest, rows = historical_manifest(source, manifest)
    else:
        rows = None
    output.mkdir(parents=True, exist_ok=False)
    if rows is not None:
        write_jsonl(output / "input-files.jsonl", rows)
    elif (source / "input-files.jsonl").is_file():
        copyfile(source / "input-files.jsonl", output / "input-files.jsonl")
    for name in ("agent-stdout.jsonl", "runtime-dependencies.txt"):
        if (source / name).is_file():
            copyfile(source / name, output / name)
    manifest.update(derived_from=str(source), report_code=code_metadata())
    generate(source, output, manifest, args.baseline)
    return 0


def unversioned_manifest(source):
    """Import older task totals when there is no pre-run provenance to verify."""
    if not (source / "tokens.jsonl").is_file():
        raise ValueError("Historical import requires tokens.jsonl")
    rows, _ = read_jsonl(source / "tokens.jsonl")
    totals = next((row for row in rows if row.get("event") == "summary"), {})
    tasks = {}
    for path in sorted((source / "tasks").glob("*/task.json")):
        task = json.loads(path.read_text())
        tasks[task["task_id"]] = {
            "task_id": task["task_id"],
            "task_type": task["task_type"],
            "reward": task["reward"],
            "directory": path.parent.name,
        }
    for row in rows:
        if row.get("event") == "task":
            task_id = row["task_id"]
            tasks.setdefault(
                task_id,
                {
                    "task_id": task_id,
                    "task_type": row.get("task_type") or "unknown",
                    "reward": row.get("reward"),
                    "directory": None,
                },
            )
    if not tasks:
        raise ValueError("No historical task inventory or per-task totals")
    return {
        "schema_version": SCHEMA_VERSION,
        "tokens_format": "task_totals",
        "input": {"tasks": list(tasks.values()), "tree_sha256": None, "content_sha256": None},
        "launch": {"status": "unknown", "model": totals.get("model")},
    }


def planning_history(args):
    samples = []
    for directory in args.run_dirs:
        manifest = load_manifest(directory)
        runtime = manifest.get("runtime")
        journal = manifest.get("journal", {})
        if (
            not runtime or not manifest.get("scoring") or not journal.get("trace_sequence_contiguous")
            or not journal.get("usage_totals_match_finish")
        ):
            raise ValueError(f"Planning history requires a complete trace and independent scoring: {directory}")
        rows, incomplete = read_jsonl(directory / "results-by-task.jsonl")
        if incomplete or len(rows) != len(manifest["input"]["tasks"]):
            raise ValueError(f"Incomplete task results: {directory}")
        for row in rows:
            initial = row.get("initial_route")
            if (
                not initial or initial != row["routing"] or row["status"] not in FULL_ATTEMPTS
                or type(row["passed"]) is not bool or not row["usage"]["usage_complete"]
                or row["local_ms"] is None
            ):
                continue
            cascade = initial["name"] == "CASCADE"
            samples.append({
                "schema_version": PLANNING_SCHEMA_VERSION,
                "source_run_id": runtime["run_id"],
                "proxy_kind": manifest["launch"]["proxy_kind"],
                "task_id": row["task_id"],
                "instruction_sha256": row["instruction_sha256"],
                "task_type": row["task_type"],
                "route": initial["name"],
                "model": initial["model"],
                "smart_model": initial["smart"]["model"] if cascade else None,
                "settings": settings_for(
                    runtime["mode"], runtime.get("generation_limits"), runtime.get("direct_publish", False),
                    row["initial_step_limit"] or runtime["max_steps"],
                    runtime.get("cascade_lambda") if cascade else None,
                ),
                "exact_tenths": row["usage"]["exact_tenths"],
                "local_ms": row["local_ms"],
                "passed": row["passed"],
            })
    if not samples:
        raise ValueError("No complete, measured and independently scored attempts for planning history")
    with args.output.open("x", encoding="utf-8") as stream:
        stream.writelines(canonical_json(row) + "\n" for row in samples)
    return 0
