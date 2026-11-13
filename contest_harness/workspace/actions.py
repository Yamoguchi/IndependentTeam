"""Track repeated operations and reuse only deterministic task-local results."""

import ast
from collections import OrderedDict, deque
from hashlib import sha256
from pathlib import Path

from contest_harness.json_codec import canonical_json
from contest_harness.workspace.paths import safe_path
from contest_harness.workspace.sandbox import CONTROL_FILES
from contest_harness.workspace.tools import tool_status

READ_TOOLS = {"read_file", "list_files", "search_text"}
FINGERPRINT_LIMIT = 2048
CACHE_RESULT_BYTES = 64 * 1024
CYCLE_WIDTH = 4
PURE_NODES = (
    ast.Call, ast.Name, ast.Load, ast.Constant, ast.Tuple, ast.List, ast.UnaryOp, ast.BinOp,
    ast.UAdd, ast.USub, ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow,
)


def literal_calculation(code):
    """Recognize a single print of literal arithmetic, with no input or state."""
    try:
        expression = ast.parse(code.strip(), mode="eval").body
    except (SyntaxError, ValueError, RecursionError):
        return False
    return (
        isinstance(expression, ast.Call) and isinstance(expression.func, ast.Name)
        and expression.func.id == "print" and not expression.keywords
        and all(not isinstance(node, (ast.Call, ast.Name)) and isinstance(node, PURE_NODES)
                for arg in expression.args for node in ast.walk(arg))
    )


def file_state(path, *, directory_entries=False, recursive=False):
    """Return a bounded change detector for visible files, or decline reuse."""
    result = sha256()
    pending = [path]
    seen = 0
    try:
        while pending:
            entry = pending.pop()
            stat = entry.lstat()
            result.update(canonical_json([
                str(entry.relative_to(path)), stat.st_mode, stat.st_size,
                stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns,
            ]).encode("utf-8", errors="surrogatepass"))
            if entry.is_dir() and not entry.is_symlink() and (recursive or directory_entries and entry == path):
                children = []
                for child in entry.iterdir():
                    seen += 1
                    if seen > FINGERPRINT_LIMIT:
                        return None
                    if child.name not in CONTROL_FILES:
                        children.append(child)
                pending.extend(sorted(children, reverse=True))
    except OSError:
        return None
    return result.hexdigest()


class ToolActions:
    def __init__(self, workspace: Path, options, trace):
        self.workspace = workspace
        self.trace = trace
        self.cache_enabled = options.features["tool_result_cache_enabled"]
        self.detect = options.features["repeated_action_detection_enabled"]
        self.repeat_limit = options.repeat_limit
        self.sequence_limit = options.sequence_limit
        self.capacity = options.cache_entries
        self.results = OrderedDict()
        self.history = deque(maxlen=max(32, (self.sequence_limit + 1) * CYCLE_WIDTH, self.repeat_limit + 1))

    def identify(self, name, args):
        reusable = name in READ_TOOLS or name == "run_python" and literal_calculation(args["code"])
        if not self.detect and (not self.cache_enabled or not reusable or not self.capacity):
            return None, False
        path = safe_path(self.workspace, args["path"]) if "path" in args else self.workspace
        if reusable and name == "run_python":
            state = "literal"
        else:
            whole_workspace = name in {"run_python", "run_python_file"}
            state = file_state(
                self.workspace if whole_workspace else path,
                directory_entries=name == "list_files", recursive=whole_workspace or name == "search_text",
            )
        if state is None:
            return None, False
        digest = sha256(canonical_json([name, args, state]).encode("utf-8", errors="surrogatepass")).hexdigest()
        return digest, reusable

    def repetition(self, key):
        if not self.detect or key is None:
            return 0, 0
        previous = list(self.history)
        count = previous.count(key)
        if not self.sequence_limit or not count:
            return count, 0
        actions = [*previous, key]
        for width in range(2, CYCLE_WIDTH + 1):
            size = width * self.sequence_limit
            if len(actions) >= size and len(set(actions[-width:])) > 1:
                if actions[-size:] == actions[-width:] * self.sequence_limit:
                    return count, width
        return count, 0

    async def run(self, name, args, execute):
        key, reusable = self.identify(name, args)
        count, width = self.repetition(key)
        bounded = (self.repeat_limit and count >= self.repeat_limit) or width
        cached = self.results.get(key) if reusable and (self.cache_enabled or bounded) else None
        if cached is None:
            output = await execute()
            if (
                key is not None and reusable and self.capacity and (self.cache_enabled or self.detect)
                and tool_status(name, output) == "ok" and len(output.encode("utf-8")) <= CACHE_RESULT_BYTES
                and self.identify(name, args)[0] == key
            ):
                self.results[key] = output
                self.results.move_to_end(key)
                if len(self.results) > self.capacity:
                    self.results.popitem(last=False)
        else:
            self.results.move_to_end(key)
            output = cached
            self.trace.tool_event("tool.cache_hit", tool=name, reason="repeat_limit" if bounded else "cache")
        if self.detect:
            if count:
                self.trace.tool_event(
                    "tool.repeated_action", tool=name, previous_calls=count, cycle_width=width, reused=cached is not None,
                )
            self.history.append(key)
        return output
