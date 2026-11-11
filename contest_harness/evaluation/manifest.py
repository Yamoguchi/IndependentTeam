"""Manifest creation, source fingerprints and prepared task copies."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from contest_harness.evaluation.artifacts import SCHEMA_VERSION, write_json, write_jsonl
from contest_harness.evaluation.inputs import (
    clean_inputs,
    fingerprint,
    inventory,
    inventory_digest,
    source_files,
)
from contest_harness.provenance import ROOT, runtime_hashes

CODE_FILES = ("Dockerfile", ".dockerignore", "requirements.txt", "requirements-linux.txt", "benchmark.py")


def now():
    return datetime.now(UTC).isoformat()


def git(*args):
    try:
        return subprocess.check_output(["git", *args], cwd=ROOT, stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def code_metadata():
    status = git("status", "--porcelain", "--untracked-files=normal")
    return {
        "git_head": git("rev-parse", "HEAD"),
        "git_dirty": bool(status) if status is not None else None,
        "files_sha256": {
            **runtime_hashes(),
            **{name: fingerprint(ROOT / name) for name in CODE_FILES if (ROOT / name).is_file()},
            **{path.relative_to(ROOT).as_posix(): fingerprint(path)
               for path in sorted(Path(__file__).parent.glob("*.py"))},
        },
    }


def prepare(args):
    root = args.run_dir
    source = args.archive or args.source_dir
    if root.is_relative_to(source) or source.is_relative_to(root):
        raise ValueError("Use separate paths for inputs and the new run directory")
    with source_files(args.archive, args.source_dir) as (names, read):
        files, tasks = clean_inputs(names, read)
    rows = inventory(files)
    root.mkdir(parents=True, exist_ok=False)
    for name, content in files.items():
        target = root / "tasks" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    write_jsonl(root / "input-files.jsonl", rows)
    tree_sha = inventory_digest(rows)
    write_json(
        root / "run-manifest.json",
        {
            "schema_version": SCHEMA_VERSION,
            "tokens_format": "per_call",
            "created_at": now(),
            "prepare_code": code_metadata(),
            "input": {
                "archive_sha256": fingerprint(args.archive) if args.archive else None,
                "tree_sha256": tree_sha,
                "content_sha256": tree_sha,
                "tasks": tasks,
            },
            "launch": {"status": "prepared"},
        },
    )
    return 0


def load_manifest(root):
    return json.loads((root / "run-manifest.json").read_text(encoding="utf-8"))
