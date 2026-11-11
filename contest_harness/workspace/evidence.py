"""Track source edits and test evidence for the current revision."""

import re
from hashlib import file_digest
from pathlib import Path

from contest_harness.workspace.answers import Check
from contest_harness.workspace.paths import PATH_PATTERN, InvalidToolCall, safe_path

TEST_DIRS = {"test", "tests", "fixture", "fixtures", "testdata", "test_data"}
SOURCE_TYPES = {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".rb", ".sh"}


def file_hash(path):
    with path.open("rb") as file:
        return file_digest(file, "sha256").digest()


def test_file(path):
    return (bool(TEST_DIRS.intersection(path.parts)) or path.name in {"test.py", "conftest.py"}
            or path.stem.startswith("test_") or path.stem.endswith(("_test", ".test", ".spec")))


class CodingEvidence:
    """Remember original inputs and test evidence for the exact edited revision."""

    def __init__(self, workspace, instruction, run_test):
        self.workspace = workspace
        self.run_test = run_test
        self.requires_tests = bool(re.search(
            r"\b(?:run|rerun|pass|verify)\b[^.!?\n]*(?:\btests?\b|test_|unittest|pytest)"
            r"|\btests?\s+(?:must|should)\s+pass\b|\btests?\s+pass\b"
            r"|\bpython(?:3(?:\.\d+)?)?\s+[\x60\"]?[^\s\x60\"]*test[^\s\x60\"]*\.py\b",
            instruction, re.I,
        ))
        positive = re.sub(r"\b(?:do not|don't|never)\s+[^.!?\n;)]*", "", instruction, flags=re.I)
        self.requires_edit = bool(re.search(
            r"\b(?:fix|repair|edit|modify|patch|implement|refactor|update)\b", positive, re.I
        ))
        self.original = {}
        self.edited = {}
        self.results = {}
        self.revision = 0
        command = rf"python(?:3(?:\.\d+)?)?\s+({PATH_PATTERN}\.py)"
        quoted = re.findall(r"\x60([^\x60\n]+)\x60", instruction)
        quoted_commands = [item for item in quoted if re.match(r"python(?:3(?:\.\d+)?)?\s", item)]
        plain = re.sub(r"\x60([^\x60\n]+)\x60", lambda m: " " if m[1] in quoted_commands else m[1], instruction)
        named_tests = {Path(name) for match in re.findall(rf"\b(?:{command}|run\s+({PATH_PATTERN}\.py))\b", plain, re.I)
                       for name in match if name}
        for item in quoted_commands:
            if match := re.fullmatch(command, item):
                named_tests.add(Path(match[1]))
        unsupported_args = any(not re.fullmatch(command, item) for item in quoted_commands) or bool(
            re.search(rf"\b{command}\s+(?:-|\d|[;&|>])", plain)
        )
        named_sources = re.findall(
            rf"\b(?:fix|repair|edit|modify|patch|implement|refactor|update)\s+[^.!?\n`]*?`?({PATH_PATTERN}\.(?:{'|'.join(sorted(s[1:] for s in SOURCE_TYPES))}))\b",
            instruction, re.I,
        )
        self.targets = {Path(path) for path in named_sources if not test_file(Path(path))}
        self.declared = bool(named_tests)
        self.requires_tests |= self.declared
        for path in workspace.rglob("*"):
            relative = path.relative_to(workspace)
            if (test_file(relative) or relative in named_tests) and path.is_file():
                try:
                    original = safe_path(workspace, relative.as_posix())
                    self.original[relative] = file_hash(original)
                except InvalidToolCall:
                    continue
        explicit_runner = re.search(r"\b(?:pytest|unittest\s+discover|python(?:3(?:\.\d+)?)?\s+-m)\b",
                                    instruction, re.I)
        if explicit_runner or unsupported_args:
            self.requires_tests = True
            self.tests = ()
        elif named_tests:
            self.tests = tuple(sorted(named_tests))
        else:
            inferred = [path for path in self.original
                        if path.suffix == ".py" and path.parent == Path(".") and path.name != "conftest.py"]
            self.tests = tuple(inferred) if len(inferred) == 1 else ()

    def before_edit(self, path):
        relative = path.relative_to(self.workspace)
        if (relative in self.original or relative in self.tests or test_file(relative)
                or path.suffix not in SOURCE_TYPES
                or any(part in {"input", "inputs", "data"} for part in relative.parts)
                or self.targets and relative not in self.targets):
            raise InvalidToolCall("Only source files within the task's edit scope are writable.")
        if relative not in self.edited:
            self.edited[relative] = file_hash(path)

    def after_edit(self):
        self.revision += 1
        self.results.clear()

    def snapshot(self):
        originals = {path: file_hash(safe_path(self.workspace, path.as_posix())) for path in self.original}
        sources = {path: file_hash(safe_path(self.workspace, path.as_posix())) for path in self.edited}
        return originals, sources

    def observe_test(self, path, output, snapshot):
        if path in self.tests and snapshot is not None and self.snapshot() == snapshot:
            self.results[path] = (self.revision, snapshot, output)

    async def verify(self, *, run_missing):
        try:
            snapshot = self.snapshot()
            originals, sources = snapshot
            if originals != self.original:
                return Check("failed", "Original test or fixture changed.")
            if self.requires_edit and sources == self.edited:
                return Check("failed", "The task requires a saved source change.")
            if not self.tests or any(path not in self.original for path in self.tests):
                return Check("unverified", "No supported original test command is available.")
            for path in self.tests:
                record = self.results.get(path)
                if record is None or record[:2] != (self.revision, snapshot):
                    if not run_missing:
                        return Check("unverified", "Original tests have not run on the latest revision.")
                    output = await self.run_test(path.as_posix())
                    self.observe_test(path, output, snapshot)
                    if self.results.get(path, ())[:2] != (self.revision, snapshot):
                        return Check("unverified", "Workspace changed during verification.")
                else:
                    output = record[2]
                status, _, diagnostics = output.partition("\n")
                if not status.startswith("Exit code ") or re.search(
                    r"Python sandbox unavailable:|\bRan 0 tests?\b|\bno tests ran\b", diagnostics, re.I
                ):
                    return Check("unverified", "Original tests did not produce a reliable result.")
                if status != "Exit code 0":
                    if status.startswith("Exit code -"):
                        return Check("unverified", "Original test process was interrupted.")
                    return Check("failed", f"Original test failed: {path.as_posix()}.")
                if not self.declared and not re.search(r"\bRan [1-9][0-9]* tests?\b", diagnostics):
                    return Check("unverified", "Inferred script exited without evidence that tests ran.")
        except FileNotFoundError:
            return Check("failed", "An original test, fixture or edited source disappeared.")
        except (OSError, InvalidToolCall):
            return Check("unverified", "Could not read original tests or the edited source.")
        return Check("confirmed", "Original tests passed on the saved revision.")
