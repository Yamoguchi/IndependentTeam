"""Score run artifacts against independent reference metadata."""

import json
import platform

from contest_harness.evaluation.artifacts import write_jsonl
from contest_harness.evaluation.inputs import (
    clean_inputs,
    fingerprint,
    inventory,
    inventory_digest,
    source_files,
)
from contest_harness.evaluation.manifest import ROOT, code_metadata, load_manifest, now
from contest_harness.evaluation.report import generate
from contest_harness.workspace.sandbox import run_python


def check_answer(read, directory, task_dir, task):
    path = task_dir / "answer.txt"
    if task_dir.is_symlink() or not path.is_file() or path.is_symlink():
        return False
    try:
        answer = path.read_text(encoding="utf-8").strip()
    except UnicodeDecodeError:
        return False
    match task["check"]:
        case "exact_int":
            try:
                return int(answer) == int(task["expected"])
            except ValueError:
                return False
        case "exact_str":
            return answer == task["expected"]
        case "unittest":
            workspace = task_dir / "workspace"
            tests = workspace / "test_mylib.py"
            if (
                answer != "DONE"
                or workspace.is_symlink()
                or not tests.is_file()
                or tests.is_symlink()
                or tests.read_bytes() != read(f"{directory}/workspace/test_mylib.py")
            ):
                return False
            return run_python(
                workspace,
                'import runpy; runpy.run_path("test_mylib.py", run_name="__main__")',
            ).startswith("Exit code 0\n")
        case _:
            raise ValueError(f"Unsupported local check: {task['check']!r}")


def check(args):
    root = args.run_dir
    manifest = load_manifest(root)
    reference = args.archive or args.source_dir
    if reference.is_relative_to(root) or root.is_relative_to(reference):
        raise ValueError("Score against a source separate from the run directory")
    if manifest["launch"]["status"] == "prepared" or not (root / "tasks").is_dir():
        raise ValueError("Check requires a run with a task directory")
    archive_sha = fingerprint(args.archive) if args.archive else None
    original_sha = manifest["input"].get("archive_sha256")
    if original_sha and archive_sha != original_sha:
        raise ValueError("Scoring archive differs from the archive recorded at preparation")
    scores = []
    metadata_files = {}
    with source_files(args.archive, args.source_dir) as (names, read):
        files, tasks = clean_inputs(names, read)
        if inventory_digest(inventory(files)) != manifest["input"]["content_sha256"]:
            raise ValueError("Scoring source differs from the prepared inputs")
        for task in tasks:
            directory = task["directory"]
            name = f"{directory}/task.json"
            metadata_files[name] = read(name)
            expected = json.loads(metadata_files[name])
            passed = check_answer(read, directory, root / "tasks" / directory, expected)
            scores.append(
                {
                    "task_id": task["task_id"],
                    "passed": passed,
                    "score": task["reward"] if passed else 0,
                }
            )
    correct = sum(row["passed"] for row in scores)
    totals = {
        "tasks": len(tasks),
        "correct": correct,
        "score": sum(row["score"] for row in scores),
        "max_score": sum(task["reward"] for task in tasks),
    }
    write_jsonl(root / "score.jsonl", [*scores, totals])
    manifest["scoring"] = {
        "checked_at": now(),
        "archive_sha256": archive_sha,
        "reference_metadata_sha256": inventory_digest(inventory(metadata_files)),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "code_sha256": {
            name: fingerprint(ROOT / name)
            for name in ("benchmark.py", "contest_harness/evaluation/scoring.py",
                         "contest_harness/evaluation/inputs.py", "contest_harness/workspace/sandbox.py")
        },
    }
    manifest["report_code"] = code_metadata()
    generate(root, root, manifest, args.baseline)
    print(json.dumps(totals))
    return 0 if correct == len(tasks) else 1
