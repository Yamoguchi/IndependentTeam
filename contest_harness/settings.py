"""Execution modes and contest runtime limits."""

import os


def configured_int(name: str, default: int, minimum: int, maximum: int) -> int:
    value = int(os.environ.get(f"HARNESS_{name}", default))
    if not minimum <= value <= maximum:
        raise ValueError(f"HARNESS_{name} must be between {minimum} and {maximum}.")
    return value

MAX_STEPS = 20
MAX_SESSIONS = 5
RUN_TIMEOUT = 3570
MIN_ADAPTIVE_OUTPUT = configured_int("MIN_ADAPTIVE_OUTPUT", 128, 32, 1024)
CONTEXT_WINDOW_BYTES = configured_int("CONTEXT_WINDOW_BYTES", 8192, 4096, 65536)
CONTEXT_STATE_BYTES = configured_int(
    "CONTEXT_STATE_BYTES", min(2304, CONTEXT_WINDOW_BYTES // 2), 512, CONTEXT_WINDOW_BYTES // 2,
)
HISTORY_MESSAGES = configured_int("HISTORY_MESSAGES", 8192, 256, 65536)
TOOL_STORE_BYTES = configured_int("TOOL_STORE_MIB", 64, 4, 128) * 1024 * 1024
GENERATION_RECOVERIES = configured_int("GENERATION_RECOVERIES", 2, 0, 5)
MIN_COST_SAMPLES = configured_int("MIN_COST_SAMPLES", 3, 1, 100)
MIN_SUCCESS_SAMPLES = configured_int("MIN_SUCCESS_SAMPLES", 20, 10, 1000)
MODES = ("baseline", "A", "B", "B+C", "B+C+D")
COMPACT_MODES = {"B+C", "B+C+D"}
