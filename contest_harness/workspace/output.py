"""Bounded tool previews and task-local access to the retained diagnostics."""

import csv
import io
import json
import os
import re
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from contest_harness.settings import TOOL_STORE_BYTES
from contest_harness.workspace.sandbox import CONTROL_FILES, EXTENDED_OUTPUT_LIMIT

PREVIEW_BYTES = 900
DETAIL_BYTES = 4096
STORE_BYTES = TOOL_STORE_BYTES
SEARCH_BYTES = 4 * 1024 * 1024
SEARCH_ENTRIES = 2048


def clipped(text, limit, *, tail=False):
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return (data[-limit:] if tail else data[:limit]).decode("utf-8", errors="ignore")


def preview(text, *, deduplicate=False):
    if deduplicate:
        lines = text.splitlines(keepends=True)
        unique = dict.fromkeys(lines)
        if len(unique) < len(lines):
            text = "".join(unique)
    if len(text.encode("utf-8")) <= PREVIEW_BYTES:
        return text
    return (
        clipped(text, PREVIEW_BYTES // 2)
        + "\n[preview shortened]\n"
        + clipped(text, PREVIEW_BYTES // 2, tail=True)
    )


def read_text(path: Path):
    with path.open("rb") as file:
        data = file.read(EXTENDED_OUTPUT_LIMIT + 1)
    if len(data) > EXTENDED_OUTPUT_LIMIT:
        raise ValueError(f"File exceeds {EXTENDED_OUTPUT_LIMIT} bytes. Use Python to read the required range.")
    return data.decode("utf-8")


def search_text(path: Path, root: Path, query: str):
    """Search literal text and keep the coordinates for every retained match."""
    def candidates():
        if path.is_file():
            yield path
            return
        for parent, dirs, files in os.walk(path):
            dirs[:] = sorted(name for name in dirs if not (Path(parent) / name).is_symlink())
            # Count directories toward the walk limit, including empty ones.
            yield None
            for name in sorted(files):
                yield Path(parent) / name

    matches = []
    kept = scanned = 0
    for index, candidate in enumerate(candidates()):
        if index >= SEARCH_ENTRIES or scanned >= SEARCH_BYTES:
            matches.append("[search scan limit reached; narrow the path]")
            break
        if (candidate is None or candidate.name in CONTROL_FILES or candidate.is_symlink()
                or not candidate.is_file()):
            continue
        remaining = SEARCH_BYTES - scanned
        with candidate.open("rb") as file:
            data = file.read(remaining + 1)
        scanned += len(data)
        complete = len(data) <= remaining
        data = data[:remaining]
        if b"\0" not in data:
            try:
                lines = data.decode("utf-8").splitlines() if complete else data.decode("utf-8", errors="ignore").splitlines()[:-1]
            except UnicodeDecodeError:
                lines = []
            for number, line in enumerate(lines, 1):
                offset = line.find(query)
                if offset < 0:
                    continue
                segment = line[max(0, offset - 100):offset + len(query) + 160]
                entry = f"{candidate.relative_to(root)}:{number}: {segment}"
                kept += len(entry.encode("utf-8")) + 1
                if kept > EXTENDED_OUTPUT_LIMIT:
                    return "\n".join([*matches, "[match limit reached; narrow the search]"])
                matches.append(entry)
        if not complete:
            matches.append("[search scan limit reached; narrow the path]")
            break
    return "\n".join(matches) or "No matches."


def test_summary(text, exit_code):
    """Recognize standard terminal summaries. Leave counts unknown on ambiguity."""
    lines = text.strip().splitlines()
    if not lines:
        return None
    unittest = re.fullmatch(r"(OK|FAILED)(?: \(([^()]*)\))?", lines[-1])
    if unittest and len(lines) >= 2:
        preceding = lines[:-1]
        if not preceding[-1]:
            preceding = preceding[:-1]
        ran = re.fullmatch(r"Ran (\d+) tests? in [\d.]+s", preceding[-1]) if preceding else None
        if ran:
            counts = parse_counts(unittest[2], r"(failures|errors|skipped|expected failures|unexpected successes)=(\d+)")
            if counts is None:
                return None
            failed = sum(counts.get(key, 0) for key in ("failures", "errors", "unexpected successes"))
            passed = int(ran[1]) - sum(counts.values())
            # Multiple subtests may fail within one test. Class/fixture errors
            # can occur outside the tests counted by 'Ran'.
            failures = [line for line in lines if line.startswith(("FAIL: ", "ERROR: "))]
            if (passed >= 0 and (exit_code == 0) == (unittest[1] == "OK") == (failed == 0)
                    and len(set(failures)) == len(failures) == counts.get("failures", 0) + counts.get("errors", 0)
                    and all(re.fullmatch(r"(?:FAIL|ERROR): test\w* \([\w.]+\)", line) for line in failures)):
                return passed, failed, int(ran[1])
            return None
    pytest = re.fullmatch(r"=+ (.+) in [\d.]+s =+", lines[-1])
    if pytest:
        counts = parse_counts(pytest[1], r"(\d+) (passed|failed|errors?|skipped|deselected|xfailed|xpassed|warnings?)", reverse=True)
        if counts is not None and counts.keys() & {"passed", "failed", "error", "errors", "skipped", "xfailed", "xpassed"}:
            # pytest's errors can refer to a test that also appears in the
            # passed/failed totals (teardown), or to collection itself.
            if counts.keys() & {"error", "errors"}:
                return None
            passed, failed = counts.get("passed", 0), counts.get("failed", 0)
            if (exit_code == 0 and not failed) or (exit_code == 1 and failed):
                return passed, failed, sum(counts.get(key, 0) for key in ("passed", "failed", "skipped", "xfailed", "xpassed"))
    return None


def parse_counts(text, pattern, *, reverse=False):
    counts = {}
    for entry in text.split(", ") if text else ():
        match = re.fullmatch(pattern, entry)
        if not match:
            return None
        key, count = (match[2], match[1]) if reverse else (match[1], match[2])
        if key in counts:
            return None
        counts[key] = int(count)
    return counts


def diagnostic(text):
    lines = text.splitlines()
    relevant = set()
    for index, line in enumerate(lines):
        if re.search(r"Traceback|^(?:FAIL|ERROR)(?:ED)?[: ]|\b(?:\w*Error|\w*Exception):|^E\s+", line):
            relevant.update(range(max(0, index - 2), min(len(lines), index + 3)))
    if relevant:
        return preview("\n".join(lines[index] for index in sorted(relevant)), deduplicate=True)
    return preview("\n".join(lines[-12:]), deduplicate=True)


def table_preview(text, delimiter):
    """Describe a complete, rectangular CSV without exposing every row."""
    rows = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
    headers = next(rows, [])
    if not headers or len(headers) > 64 or any(not name or len(name) > 96 for name in headers):
        return None
    examples = []
    missing = [0] * len(headers)
    count = 0
    try:
        for row in rows:
            if not row:
                continue
            if len(row) != len(headers):
                return None
            count += 1
            if len(examples) < 2:
                examples.append([clipped(value, 40) for value in row])
            for index, value in enumerate(row):
                missing[index] += not value.strip()
    except csv.Error:
        return None
    schema = {"rows": count, "columns": headers, "blank_cells_by_column": missing}
    result = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    if len(result.encode()) > PREVIEW_BYTES:
        return None
    schema["example_rows"] = examples
    with_examples = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
    return with_examples if len(with_examples.encode()) <= PREVIEW_BYTES else result


class OutputStoreFull(ValueError):
    """The per-task quota cannot retain another result."""


class ToolOutputs:
    def __init__(self, trace):
        self.temporary = TemporaryDirectory(prefix="harness-output-", dir="/tmp")
        self.root = Path(self.temporary.name)
        self.trace = trace
        self.entries = {}
        self.handles = {}
        self.labels = {}
        self.size = 0
        self.sequence = 0

    def close(self):
        self.temporary.cleanup()

    def remember(self, text, *, label=None):
        data = text.encode("utf-8")
        fingerprint = sha256(data).digest()
        if (handle := self.handles.get(fingerprint)) is not None:
            if label:
                self.labels.setdefault(handle, clipped(label, 180))
            return handle, len(data), True
        if self.size + len(data) > STORE_BYTES:
            raise OutputStoreFull("Tool output storage is full. Narrow the requested file, search or command output.")
        self.sequence += 1
        handle = f"out-{self.sequence}"
        (self.root / handle).write_bytes(data)
        self.entries[handle] = (fingerprint, len(data))
        self.handles[fingerprint] = handle
        if label:
            self.labels[handle] = clipped(label, 180)
        self.size += len(data)
        return handle, len(data), False

    def compact(self, tool, output, *, delimiter=None, label=None):
        status, _, body = output.partition("\n")
        python = tool in {"run_python", "run_python_file"}
        if not python:
            status, body = "", output
        test = tool == "run_python_file"
        code = re.fullmatch(r"Exit code (-?\d+)", status) if python else None
        summary = test_summary(body, int(code[1])) if test and code else None
        # Plain source is kept verbatim. Repeated lines are collapsed only in
        # console previews. Preserve exact short results unless duplicated.
        if not test and not body:
            return output
        handle, size, repeated = self.remember(body, label=label)
        locator = f"[Details {handle}, {size} UTF-8 bytes]"
        if test:
            outcome = "failed" if not code or code[1] != "0" else "passed" if summary else "unknown"
            status += f"\nResult: {outcome}"
            if summary is not None:
                passed, failed, total = summary
                status += f" ({passed} passed, {failed} failed, {total} total)"
            else:
                status += " (test counts unknown)"
            if summary is None or (code and code[1] != "0"):
                visible = diagnostic(body) if not repeated else ""
            else:
                visible = ""
        elif repeated:
            visible = ""
        elif python and code and code[1] != "0":
            visible = diagnostic(body)
        elif delimiter and size > PREVIEW_BYTES:
            try:
                visible = table_preview(body, delimiter) or preview(body)
            except csv.Error:
                visible = preview(body)
        else:
            visible = preview(body, deduplicate=python)
        if not test and not repeated and visible == body:
            return output
        result = "\n".join(part for part in (status, locator + (" [repeated output]" if repeated else ""), visible) if part)
        self.trace.tool_event(
            "tool.output_compacted", tool=tool, handle=handle, repeated=repeated,
            raw_bytes=len(output.encode("utf-8")), stored_bytes=size,
            preview_bytes=len(result.encode("utf-8")),
        )
        return result

    def details(self, handle, span):
        if handle == "index":
            listing = "\n".join(
                f"{key} {self.labels.get(key, 'output')} ({size} bytes)"
                for key, (_, size) in self.entries.items()
            ).encode()
            if not listing:
                return "No retained outputs."
            start, end = self.byte_range(span, len(listing))
            return f"index bytes {start}:{end}/{len(listing)}\n{listing[start:end].decode(errors='replace')}"
        if handle not in self.entries:
            raise ValueError("Unknown or expired output id. Rerun its tool to capture a fresh result.")
        path = self.root / handle
        _, size = self.entries[handle]
        if span == "diagnostic":
            return f"{handle} diagnostic:\n{diagnostic(path.read_text(encoding='utf-8'))}"
        start, end = self.byte_range(span, size)
        with path.open("rb") as file:
            file.seek(start)
            data = file.read(end - start)
        return f"{handle} bytes {start}:{end}/{size}\n{data.decode(errors='replace')}"

    @staticmethod
    def byte_range(span, size):
        match = re.fullmatch(r"(\d{1,9}):(\d{1,9})", span)
        if not match:
            raise ValueError("Use a UTF-8 byte range such as 0:1024, or diagnostic.")
        start, end = map(int, match.groups())
        if not start < end or end - start > DETAIL_BYTES or start >= size:
            raise ValueError(f"Select up to {DETAIL_BYTES} bytes inside this {size}-byte output.")
        return start, min(end, size)
