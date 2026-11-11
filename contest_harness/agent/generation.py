"""Task-local generation profiles and validation of native function calls."""

from contest_harness.json_codec import strict_json_loads
from contest_harness.settings import GENERATION_RECOVERIES

DEFAULT_LIMITS = {
    "short": 160,
    "analysis": 768,
    "structured": 384,
    "code": 1536,
    "debug": 2048,
}
RECOVERY_HINT = (
    "Your previous completion could not be used ({reason}). Return a complete call to an available "
    "tool with its declared arguments. If the action is too large, use a smaller step."
)


class GenerationFailed(ValueError):
    pass


def profile_limits(overrides, cap):
    limits = {name: min(value, cap) for name, value in DEFAULT_LIMITS.items()}
    seen = set()
    for entry in overrides:
        name, _, value = entry.partition("=")
        if name not in limits or name in seen or not value.isascii() or not value.isdecimal():
            raise ValueError("Use each --generation-limit PROFILE=TOKENS at most once.")
        count = int(value)
        if not 1 <= count <= cap:
            raise ValueError(f"Generation limit for {name} must be between 1 and {cap}.")
        seen.add(name)
        limits[name] = count
    return limits


def valid_arguments(schema, arguments):
    return (
        schema is not None
        and isinstance(arguments, dict)
        and arguments.keys() == schema
        and all(isinstance(value, str) for value in arguments.values())
    )


class GenerationPolicy:
    def __init__(self, task_type, schemas, limits, *, terminal_signals=()):
        self.task_type = task_type
        self.schemas = schemas
        self.limits = limits
        self.last = None
        self.recoveries = 0
        self.failed = False
        self.terminal_signals = terminal_signals

    def observe_tool(self, name, status):
        self.last = name, status

    def profile(self):
        if self.last is None:
            return "code" if self.task_type == "coding" else "analysis"
        tool, status = self.last
        if status != "ok":
            return "debug"
        if self.task_type == "coding":
            if tool == "run_python_file":
                return "short"
            if tool == "write_file":
                return "structured"
            return "code"
        if tool == "run_python":
            return "short"
        return "structured"

    def rejection(self, response, finish_reason):
        if finish_reason == "length":
            return "length"
        if finish_reason == "content_filter":
            return "content_filter"
        if finish_reason not in {"stop", "tool_calls"}:
            return "unexpected_finish_reason"
        calls = response.tool_calls or []
        if not calls:
            content = response.content
            if finish_reason == "stop" and isinstance(content, str) and content.strip() in self.terminal_signals:
                return None
            return "missing_tool_call"
        if any(not call.id for call in calls) or len({call.id for call in calls}) != len(calls):
            return "invalid_tool_call_ids"
        for call in calls:
            try:
                arguments = strict_json_loads(call.arguments)
            except (ValueError, TypeError, RecursionError):
                return "malformed_arguments"
            if not valid_arguments(self.schemas.get(call.name), arguments):
                return "invalid_arguments"
            if call.name == "submit_answer" and not arguments["answer"].strip():
                return "empty_answer"
        return None

    def recovery_limit(self, reason, previous):
        if self.recoveries >= GENERATION_RECOVERIES or reason == "content_filter":
            return None
        return max(previous, self.limits["debug"], self.limits["code"])
