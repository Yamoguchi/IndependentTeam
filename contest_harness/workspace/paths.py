"""Validate paths before opening task workspace files."""

from pathlib import Path

from contest_harness.workspace.sandbox import CONTROL_FILES

PATH_PATTERN = r"[-\w./]+"


class InvalidToolCall(ValueError):
    pass


def safe_path(workspace, name):
    relative = Path(name)
    if not name.strip() or "\x00" in name or relative.is_absolute() or ".." in relative.parts:
        raise InvalidToolCall("Use a nonempty relative path inside the working directory.")
    path = workspace
    for part in relative.parts:
        path = path / part
        if part in CONTROL_FILES or path.is_symlink():
            raise InvalidToolCall("Control files and symbolic links are outside the task inputs.")
    return path
