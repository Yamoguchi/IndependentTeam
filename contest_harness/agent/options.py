"""Independent switches for the single-model solver."""

from dataclasses import dataclass, field

from contest_harness.settings import MAX_STEPS

FEATURES = (
    "tool_filtering_enabled",
    "context_management_enabled",
    "tool_output_reduction_enabled",
    "output_control_enabled",
    "local_first_enabled",
    "tool_result_cache_enabled",
    "repeated_action_detection_enabled",
    "iteration_limits_enabled",
)
ITERATIONS = {"math": 16, "search": 24, "data_analysis": 24, "coding": 40, "default": MAX_STEPS}


def default_features():
    return {**dict.fromkeys(FEATURES, True), "tool_output_reduction_enabled": None}


def add_solver_options(parser):
    parser.add_argument(
        "--feature", action="append", default=[], metavar="NAME=on|off",
        help="Override one optimization in minimal mode. Can be repeated.",
    )
    parser.add_argument(
        "--max-iterations", action="append", default=[], metavar="TYPE=N",
        help="Override the ReAct iteration cap for a task type. Zero disables the cap.",
    )
    parser.add_argument("--repeat-limit", type=int, default=None,
                        help="Reuse a deterministic result after N identical actions (0 disables).")
    parser.add_argument("--sequence-limit", type=int, default=None,
                        help="Reuse deterministic results after N identical sequences (0 disables).")
    parser.add_argument("--cache-entries", type=int, default=None,
                        help="Maximum retained deterministic results per task (0 disables).")


@dataclass(frozen=True)
class SolverOptions:
    features: dict = field(default_factory=default_features)
    iterations: dict = field(default_factory=lambda: ITERATIONS.copy())
    repeat_limit: int = 3
    sequence_limit: int = 2
    cache_entries: int = 32

    @classmethod
    def parse(cls, args):
        defaults = cls()
        features = defaults.features.copy()
        seen = set()
        for entry in args.feature:
            name, _, value = entry.partition("=")
            if name not in features or name in seen or value not in {"on", "off"}:
                raise ValueError("Use --feature NAME=on|off once per optimization: " + ", ".join(FEATURES))
            seen.add(name)
            features[name] = value == "on"
        iterations = defaults.iterations.copy()
        seen.clear()
        for entry in args.max_iterations:
            name, _, value = entry.partition("=")
            if name not in iterations or name in seen or not value.isascii() or not value.isdecimal() or len(value) > 4:
                raise ValueError("Use --max-iterations TYPE=0..9999 once per task type.")
            seen.add(name)
            iterations[name] = int(value)
        values = {}
        for name, maximum in (("repeat_limit", 100), ("sequence_limit", 100), ("cache_entries", 128)):
            value = getattr(args, name)
            if value is None:
                continue
            if not 0 <= value <= maximum:
                raise ValueError(f"--{name.replace('_', '-')} must be between 0 and {maximum}.")
            values[name] = value
        return cls(features, iterations, **values)

    def steps(self, task_type):
        if self.features["iteration_limits_enabled"]:
            limit = self.iterations.get(task_type, self.iterations["default"])
            if limit:
                return limit
        # openJiuwen accepts only integers here. The run deadline and budget
        # remain in force and bound execution long before this SDK sentinel.
        return 2**31 - 1

    def compact_output(self, task_type):
        configured = self.features["tool_output_reduction_enabled"]
        return task_type != "data_analysis" if configured is None else configured
