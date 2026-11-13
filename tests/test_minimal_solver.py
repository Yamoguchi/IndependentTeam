"""Regression coverage for independent minimal-mode tool and context controls."""

from io import StringIO
from types import SimpleNamespace

import pytest
from openjiuwen.core.context_engine.base import ContextWindow
from openjiuwen.core.foundation.llm import AssistantMessage, ToolMessage, UserMessage
from test_budget import KEY, MODEL, proxy
from test_task_context import action, add_task, fixture_usage, response

from contest_harness.agent.context import WINDOW_BYTES, TaskContext
from contest_harness.agent.model import TracedModel
from contest_harness.agent.options import SolverOptions
from contest_harness.planning.routing import features_for
from contest_harness.scheduler import run
from contest_harness.telemetry.trace import RunTrace
from contest_harness.workspace.actions import ToolActions
from contest_harness.workspace.output import ToolOutputs
from contest_harness.workspace.tools import python_retry_hint, toolset


def options(**features):
    defaults = SolverOptions()
    return SolverOptions(features={**defaults.features, **features})


def test_data_tool_filter_and_local_first_are_independent():
    local = options(tool_output_reduction_enabled=False, context_management_enabled=False)
    regular = options(local_first_enabled=False)
    tools, prompt = toolset("data_analysis", "minimal", options=local)
    assert {item["name"] for item in tools} == {"list_files", "read_file", "run_python", "submit_answer"}
    assert "csv.DictReader" in prompt
    tools, prompt = toolset("data_analysis", "minimal", options=regular)
    assert any(item["name"] == "output_details" for item in tools)
    assert "csv.DictReader" not in prompt


def test_bash_guidance_is_scoped_to_math():
    for kind in ("math", "data_analysis", "search", "coding"):
        _, prompt = toolset(kind, "minimal", options=options())
        assert ("including when the task requests Bash or python3" in prompt) == (kind == "math")


def test_coding_filters_listing_only_when_all_paths_are_named(tmp_path):
    for name in ("mylib.py", "test_mylib.py"):
        (tmp_path / name).touch()
    task = {"task_type": "coding", "instruction": "Fix mylib.py. Run `python3 test_mylib.py`."}

    def tools_for(flags):
        definitions, _ = toolset("coding", "minimal", features=features_for(task, tmp_path), options=flags)
        return {item["name"] for item in definitions}

    assert "list_files" not in tools_for(options())
    assert "list_files" in tools_for(options(tool_filtering_enabled=False))
    task["instruction"] += " Inspect the directory."
    assert "list_files" in tools_for(options())
    task["instruction"] = "Fix test_mylib.py."
    assert "list_files" in tools_for(options())


def test_python_retry_hint_recognizes_tool_contract_errors(tmp_path):
    header = 'Exit code 1\nTraceback (most recent call last):\n  File "<task>", line 1\n'
    syntax = header + "SyntaxError: invalid syntax\n"
    printed, hint = python_retry_hint("python3 -c 'print(7)'", syntax, tmp_path)
    assert printed == "" and "directly to run_python" in hint
    assert python_retry_hint("print(", syntax, tmp_path) is None
    missing = header + "FileNotFoundError: [Errno 2] No such file or directory: 'workspace/input.csv'\n"
    assert python_retry_hint("open('workspace/input.csv')", missing, tmp_path) is None
    (tmp_path / "input.csv").write_text("value\n7\n")
    _, hint = python_retry_hint("open('workspace/input.csv')", missing, tmp_path)
    assert "Use 'input.csv'" in hint
    denied = header + "PermissionError: [Errno 13] Permission denied: 'input.csv'\n"
    assert python_retry_hint("open('input.csv', 'w')", denied, tmp_path) is None


async def test_data_python_retry_preserves_full_traceback(tmp_path):
    add_task(tmp_path, "data", "data_analysis", "Inspect the input and return only the integer.",
             7, "exact_int", 1, {"input.csv": "value\n7\n"})
    messages = [
        response(1, [action("run_python", code="print(7, flush=True); print(8); open('answer.txt', 'w').write('7')")]),
        response(2, [action("output_details", id="out-1", range="0:4096")]),
        response(3, [action("run_python", code="print(open('input.csv').read().splitlines()[1])")]),
        "7",
    ]
    trace = RunTrace(StringIO())
    with proxy(*[(200, fixture_usage, message, None) for message in messages]) as (url, received):
        assert await run(TracedModel(trace, MODEL, url, KEY), MODEL, tmp_path, trace, "minimal", options=options()) == 0
    initial, retry, details, final = [received.get_nowait() for _ in messages]
    assert all(row["role"] != "tool" for row in initial["messages"])
    brief = retry["messages"][-1]["content"]
    full = details["messages"][-1]["content"]
    assert "\n7\n8\nPermissionError: submit_answer writes answer.txt" in brief
    assert "Traceback" not in brief and "Traceback" in full and len(brief.encode()) < len(full.encode())
    assert final["messages"][-1]["content"] == "Exit code 0\n7\n"
    assert (tmp_path / "data/answer.txt").read_text() == "7\n"


@pytest.mark.parametrize("invalid_write", [False, True])
async def test_direct_publish_after_successful_edit_batch_only(tmp_path, invalid_write):
    original_test = "from mylib import value\nassert value() == 7\n"
    add_task(tmp_path, "code", "coding", "Fix mylib.py. Run python3 test_mylib.py after edits. Write only the word DONE.",
             "DONE", "unittest", 1, {"mylib.py": "def value(): return 0\n", "test_mylib.py": original_test})
    directory = tmp_path / "code"
    fixed = action("write_file", path="mylib.py", content="def value(): return 7\n")
    protected = action("write_file", path="test_mylib.py", content="")
    check = action("run_python_file", path="test_mylib.py")

    def submit(_request):
        assert not (directory / "answer.txt").exists()
        return "DONE"

    messages = (
        [response(1, [fixed]), response(2, [protected, check]), submit] if invalid_write else
        [response(1, [fixed, check])]
    )
    trace = RunTrace(StringIO())
    with proxy(*[(200, fixture_usage, message, None) for message in messages]) as (url, received):
        assert await run(TracedModel(trace, MODEL, url, KEY), MODEL, tmp_path, trace, "minimal",
                         options=options(), direct_publish=True) == 0
    assert received.qsize() == len(messages)
    assert (directory / "answer.txt").read_bytes() == b"DONE\n"
    assert (directory / "workspace/test_mylib.py").read_text() == original_test


async def test_cache_respects_edits_and_never_reuses_test_execution(tmp_path):
    file = tmp_path / "data.csv"
    file.write_text("first")
    trace = RunTrace(StringIO())
    actions = ToolActions(tmp_path, options(), trace)
    calls = []

    async def read():
        value = file.read_text()
        calls.append(value)
        return value

    args = {"path": file.name}
    assert await actions.run("read_file", args, read) == "first"
    assert await actions.run("read_file", args, read) == "first"
    file.write_text("other")
    assert await actions.run("read_file", args, read) == "other"
    assert calls == ["first", "other"]

    checks = []

    async def run_test():
        checks.append(file.read_text())
        return "Exit code 0\n"

    for _ in range(4):
        await actions.run("run_python_file", {"path": "test_data.py"}, run_test)
    assert checks == ["other"] * 4


async def test_cycle_limit_can_reuse_a_read_with_cache_switched_off(tmp_path):
    paths = [tmp_path / name for name in ("a.txt", "b.txt")]
    for file in paths:
        file.write_text(file.name)
    trace = RunTrace(StringIO())
    actions = ToolActions(tmp_path, options(tool_result_cache_enabled=False), trace)
    calls = []
    for file in (*paths, *paths):
        async def read():
            calls.append(file.name)
            return file.read_text()

        assert await actions.run("read_file", {"path": file.name}, read) == file.name
    assert calls == ["a.txt", "b.txt", "a.txt"]


def test_csv_summary_and_context_excerpt_keep_retrievable_output():
    trace = RunTrace(StringIO())
    outputs = ToolOutputs(trace)
    try:
        csv = "region,units\n" + "East,1\n" * 160 + "West,\n"
        result = outputs.compact("read_file", csv, delimiter=",")
        assert '"rows":161' in result and '"blank_cells_by_column":[0,1]' in result
        assert outputs.details("out-1", "0:4096").split("\n", 1)[1] == csv
        context = TaskContext("Find the total.", False, trace)
        context.outputs = outputs
        assert "Details out-1" in context.output_excerpt(csv)
        assert "out-1" in outputs.details("index", "0:4096")
    finally:
        outputs.close()


async def test_context_keeps_the_latest_large_tool_exchange_paired():
    trace = RunTrace(None)
    outputs = ToolOutputs(trace)
    try:
        instruction = "Find the last value in data.csv."
        context = TaskContext(instruction, False, trace)
        context.outputs = outputs
        assistant = AssistantMessage(**response(1, [action("read_file", path="data.csv")]))
        raw = "x" * WINDOW_BYTES + "\nlast value"
        reply = ToolMessage(content=raw, tool_call_id=assistant.tool_calls[0].id)
        context.observe_tool("read_file", {"path": "data.csv"}, reply, "ok", "data.csv", executed=True)
        window = ContextWindow(context_messages=[UserMessage(content=instruction), assistant, reply])
        selected = (await context.select_window(None, window)).context_messages
        assert selected[-2] == assistant
        assert selected[-1].tool_call_id == assistant.tool_calls[0].id
        assert "Details out-1" in selected[-1].content
        assert selected[-1].content.endswith("last value")
        assert outputs.details("out-1", f"{WINDOW_BYTES}:{WINDOW_BYTES + 11}").endswith("last value")
    finally:
        outputs.close()


def test_iteration_caps_can_be_overridden_or_disabled():
    args = SimpleNamespace(feature=[], max_iterations=["math=0", "coding=55"], repeat_limit=None,
                           sequence_limit=None, cache_entries=None)
    configured = SolverOptions.parse(args)
    assert configured.steps("coding") == 55
    assert configured.steps("math") > configured.steps("search")
    args.feature = ["iteration_limits_enabled=off"]
    assert SolverOptions.parse(args).steps("coding") == configured.steps("math")
