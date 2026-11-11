"""Task discovery, workspace paths and atomic writes."""

import os
from pathlib import Path
from tempfile import NamedTemporaryFile


def atomic_write(path: Path, value: str) -> None:
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as file:
        temporary = Path(file.name)
        try:
            file.write(value)
            file.flush()
            os.fsync(file.fileno())
            file.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def workspace_for(task_dir):
    workspace = task_dir / "workspace"
    if workspace.is_symlink():
        raise ValueError("workspace cannot be a symbolic link")
    return workspace if workspace.is_dir() else task_dir
