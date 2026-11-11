"""Read and write benchmark JSON, JSONL and text artifacts."""

import json
from pathlib import Path
from tempfile import NamedTemporaryFile

from contest_harness.json_codec import canonical_json

SCHEMA_VERSION = 1


def write_text(path, content):
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            file.write(content)
            file.close()
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def write_json(path, data):
    write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def write_jsonl(path, rows):
    write_text(path, "".join(canonical_json(row) + "\n" for row in rows))


def read_jsonl(path):
    """Recover the valid prefix after a hard kill during the last write."""
    if not path.is_file():
        return [], False
    lines = path.read_bytes().splitlines(keepends=True)
    rows = []
    for index, line in enumerate(lines):
        try:
            row = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if index == len(lines) - 1 and not line.endswith(b"\n"):
                return rows, True
            raise ValueError(f"Invalid JSONL in {path}:{index + 1}") from None
        if not isinstance(row, dict):
            raise ValueError(f"Expected a JSON object in {path}:{index + 1}")
        rows.append(row)
    return rows, False
