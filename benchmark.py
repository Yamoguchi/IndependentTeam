"""Prepare and independently score the competition's public example archive."""

import argparse
import json
import sys
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from task_sandbox import run_python


def sample_files(archive: ZipFile):
    for entry in archive.infolist():
        parts = PurePosixPath(entry.filename).parts
        if entry.is_dir() or len(parts) < 3 or parts[0] != "task-dir":
            continue
        if any(part.startswith(".") for part in parts[1:]):
            continue
        yield Path(*parts[1:]), entry


def prepare(archive: ZipFile, tasks_dir: Path) -> int:
    tasks_dir.mkdir(parents=True, exist_ok=False)
    for relative, entry in sample_files(archive):
        if relative.name == "answer.txt":
            continue
        target = tasks_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        content = archive.read(entry)
        if relative.name == "task.json":
            metadata = json.loads(content)
            content = json.dumps(
                {
                    key: metadata[key]
                    for key in ("task_id", "task_type", "instruction", "reward")
                }
            ).encode()
        target.write_bytes(content)
    return 0


def check_answer(archive: ZipFile, task_dir: Path, task: dict) -> bool:
    path = task_dir / "answer.txt"
    if not path.is_file() or path.is_symlink():
        return False
    answer = path.read_text().strip()
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
            if answer != "DONE" or not tests.is_file() or tests.is_symlink():
                return False
            original = archive.read(f"task-dir/{task_dir.name}/workspace/test_mylib.py")
            if tests.read_bytes() != original:
                return False
            return run_python(
                workspace,
                'import runpy; runpy.run_path("test_mylib.py", run_name="__main__")',
            ).startswith("Exit code 0\n")
        case _:
            raise ValueError("Unknown public example check")


def check(archive: ZipFile, tasks_dir: Path) -> int:
    correct = score = possible = tasks = 0
    for relative, entry in sorted(sample_files(archive)):
        if relative.name != "task.json":
            continue
        task = json.loads(archive.read(entry))
        passed = check_answer(archive, tasks_dir / relative.parent, task)
        reward = task["reward"] if passed else 0
        correct += passed
        score += reward
        possible += task["reward"]
        tasks += 1
        print(
            json.dumps({"task_id": task["task_id"], "passed": passed, "score": reward})
        )
    print(
        json.dumps(
            {"tasks": tasks, "correct": correct, "score": score, "max_score": possible}
        )
    )
    return 0 if tasks and correct == tasks else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "check"))
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--tasks-dir", type=Path, required=True)
    args = parser.parse_args()
    with ZipFile(args.archive) as archive:
        operation = prepare if args.command == "prepare" else check
        return operation(archive, args.tasks_dir.resolve())


if __name__ == "__main__":
    sys.exit(main())
