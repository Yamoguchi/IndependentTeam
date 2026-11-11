"""Input inventories for the local benchmark. The agent never imports this module."""

import json
import stat
from contextlib import contextmanager
from hashlib import sha256
from math import isfinite
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from contest_harness.json_codec import canonical_json

TASK_FIELDS = ("task_id", "task_type", "instruction", "reward")


def digest(data):
    return sha256(data).hexdigest()


def fingerprint(path):
    return digest(path.read_bytes())


def inventory(files):
    return [
        {"path": name, "bytes": len(content), "sha256": digest(content)}
        for name, content in sorted(files.items())
    ]


def inventory_digest(rows):
    return digest("".join(canonical_json(row) + "\n" for row in rows).encode())


def included(name):
    return not any(part.startswith(".") or part == "__pycache__" for part in name.parts)


@contextmanager
def source_files(archive, source_dir=None):
    if source_dir is not None:
        names = []
        for path in source_dir.rglob("*"):
            name = path.relative_to(source_dir)
            if included(name) and (path.is_symlink() or path.is_file()):
                names.append(name.as_posix())

        def read(name):
            path = source_dir / name
            if path.is_symlink():
                raise ValueError(f"Symbolic link in input: {name}")
            return path.read_bytes()

        yield names, read
        return

    with ZipFile(archive) as zipped:
        entries = {}
        for entry in zipped.infolist():
            path = PurePosixPath(entry.filename)
            if entry.is_dir() or len(path.parts) < 3 or path.parts[0] != "task-dir":
                continue
            name = PurePosixPath(*path.parts[1:])
            if not included(name):
                continue
            if str(path) != entry.filename or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError(f"Invalid archive entry: {entry.filename}")
            if str(name) in entries:
                raise ValueError(f"Duplicate archive entry: {entry.filename}")
            entries[str(name)] = entry
        yield entries, lambda name: zipped.read(entries[name])


def clean_inputs(names, read):
    directories = {
        path.parts[0]
        for name in names
        if len((path := PurePosixPath(name)).parts) == 2 and path.name == "task.json"
    }
    files = {}
    tasks = []
    seen = set()
    for name in sorted(names):
        path = PurePosixPath(name)
        if path.parts[0] not in directories or path.name == "answer.txt":
            continue
        content = read(name)
        if len(path.parts) == 2 and path.name == "task.json":
            metadata = json.loads(content)
            task = {key: metadata[key] for key in TASK_FIELDS}
            if not isinstance(task["task_id"], str) or task["task_id"] in seen:
                raise ValueError(f"Invalid or duplicate task_id: {name}")
            if (
                not isinstance(task["task_type"], str)
                or not isinstance(task["instruction"], str)
                or type(task["reward"]) not in (int, float)
                or not isfinite(task["reward"])
            ):
                raise ValueError(f"Invalid task_type, instruction or reward: {name}")
            seen.add(task["task_id"])
            tasks.append(
                {
                    "directory": path.parts[0],
                    "task_id": task["task_id"],
                    "task_type": task["task_type"],
                    "reward": task["reward"],
                    "instruction_sha256": digest(task["instruction"].encode()),
                }
            )
            content = (canonical_json(task) + "\n").encode()
        files[name] = content
    if not tasks:
        raise ValueError("No task directories with task.json")
    return files, tasks


def actual_inventory(root):
    rows = {}
    directories = set()
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError(f"Unexpected filesystem entry in prepared inputs: {path}")
        if path.is_file():
            rows[path.relative_to(root).as_posix()] = path.read_bytes()
        else:
            directories.add(path.relative_to(root))
    expected = {parent for name in rows for parent in Path(name).parents if parent != Path(".")}
    if directories != expected:
        raise ValueError("Unexpected empty directories in prepared inputs")
    return inventory(rows)


def historical_input(source, legacy):
    """Verify the historical (unfiltered) inventory and derive clean content identity."""
    archive = source / "input-task-dir.zip"
    index = source / "input-files.jsonl"
    if fingerprint(archive) != legacy["input_zip_sha256"]:
        raise ValueError("Historical archive SHA-256 differs from the manifest")
    if fingerprint(index) != legacy["input_tree_sha256"]:
        raise ValueError("Historical input inventory SHA-256 differs from the manifest")
    rows = [json.loads(line) for line in index.read_text().splitlines()]
    with ZipFile(archive) as zipped:
        archived = {item.filename for item in zipped.infolist() if not item.is_dir()}
        if archived != {row["path"] for row in rows} or len(archived) != len(rows):
            raise ValueError("Historical archive paths differ from the inventory")
        for row in rows:
            data = zipped.read(row["path"])
            if len(data) != row["bytes"] or digest(data) != row["sha256"]:
                raise ValueError(f"Historical input SHA-256 mismatch: {row['path']}")
    with source_files(archive) as (names, read):
        files, tasks = clean_inputs(names, read)
        # Historical prepare serialized the filtered metadata with json.dumps defaults.
        old_files = dict(files)
        for task in tasks:
            name = f"{task['directory']}/task.json"
            metadata = json.loads(files[name])
            old_files[name] = json.dumps({key: metadata[key] for key in TASK_FIELDS}).encode()
    return {
        "archive_sha256": legacy["input_zip_sha256"],
        "historical_unfiltered_tree_sha256": legacy["input_tree_sha256"],
        "tree_sha256": inventory_digest(inventory(old_files)),
        "content_sha256": inventory_digest(inventory(files)),
        "tasks": tasks,
    }, inventory(old_files)
