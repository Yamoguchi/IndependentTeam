"""Read only the four fields in the competition task contract."""

from math import isfinite
from pathlib import Path

from contest_harness.json_codec import strict_json_loads


def load_task(path):
    metadata = strict_json_loads(path.read_text(encoding="utf-8"))
    required = ("task_id", "task_type", "instruction", "reward")
    if not isinstance(metadata, dict) or any(key not in metadata for key in required):
        raise ValueError("Task metadata must contain task_id, task_type, instruction and reward.")
    if any(not isinstance(metadata[key], str) or not metadata[key].strip() for key in required[:3]):
        raise ValueError("Task identifiers and instruction must be nonblank strings.")
    reward = metadata["reward"]
    if type(reward) not in {int, float} or isinstance(reward, float) and not isfinite(reward):
        raise ValueError("Task reward must be a finite number.")
    return {key: metadata[key] for key in required}


def discover_tasks(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.iterdir()
        if not path.is_symlink()
        and (path / "task.json").is_file()
        and not (path / "task.json").is_symlink()
    )
