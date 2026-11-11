"""Stage 04 integration checks against the actual final HTTP payload."""

import json
from math import ceil
from types import SimpleNamespace

from openjiuwen.core.context_engine.base import ContextWindow
from openjiuwen.core.foundation.llm import AssistantMessage, ToolMessage
from test_budget import KEY, MODEL, proxy

from contest_harness.agent.context import STATE_PREFIX, WINDOW_BYTES, TaskContext
from contest_harness.evaluation.artifacts import read_jsonl
from contest_harness.evaluation.launch import run
from contest_harness.evaluation.manifest import prepare
from contest_harness.evaluation.scoring import check
from contest_harness.json_codec import canonical_json
from contest_harness.telemetry.trace import RunTrace, describe


def fixture_usage(request):
    """Synthetic usage to exercise reporting. This is a size function, not a tokenizer."""
    prompt = ceil(describe(request)["utf8_bytes"] / 4)
    return {"prompt_tokens": prompt, "completion_tokens": 10, "total_tokens": prompt + 10}


def action(name, **arguments):
    return name, arguments


def response(index, actions):
    return {
        "role": "assistant",
        "content": "Plan: trace the references." if actions[0][1].get("path") == "index.md" else "",
        "tool_calls": [
            {
                "id": f"call-{index}-{offset}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
            for offset, (name, arguments) in enumerate(actions)
        ],
    }


def add_task(root, name, kind, instruction, expected, check_type, reward, files=None):
    task = root / name
    task.mkdir(parents=True)
    (task / "task.json").write_text(
        json.dumps(
            {
                "task_id": name,
                "task_type": kind,
                "instruction": instruction,
                "reward": reward,
                "expected": expected,
                "check": check_type,
            }
        ),
        encoding="utf-8",
    )
    for name, content in (files or {}).items():
        path = task / "workspace" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def assert_paired(messages):
    pending = []
    for message in messages:
        if message["role"] == "tool":
            pending.remove(message["tool_call_id"])
        else:
            assert not pending
            pending.extend(call["id"] for call in message.get("tool_calls", []))
    assert not pending


def data(request):
    return "\n".join(message["content"] or "" for message in request["messages"])


def states(request):
    return [
        json.loads(message["content"][len(STATE_PREFIX) :])
        for message in request["messages"]
        if (message["content"] or "").startswith(STATE_PREFIX)
    ]


async def test_current_check_survives_history_rotation_and_an_incomplete_group():
    instruction = "Keep the supplied tests intact and run them after each edit."
    state = TaskContext(instruction, coding=True, trace=RunTrace(None, limit_tenths=5000))
    write = ToolMessage(content="File saved.", tool_call_id="1")
    failure = ToolMessage(
        content="Exit code 1\n" + ("test log " * 5000) + "\nAssertionError: current failure",
        tool_call_id="2",
    )
    state.observe_tool("write_file", "{}", write, "ok", "lib.py", executed=True)
    state.observe_tool("run_python_file", "{}", failure, "error", "test_lib.py", executed=True)
    state.observe_response(AssistantMessage(content="Candidate: first patch"))
    window = ContextWindow(
        context_messages=[
            ToolMessage(content="orphan", tool_call_id="2"),
            AssistantMessage(**response(3, [action("read_file", path="lib.py")])),
        ]
    )
    compact = await state.select_window(None, window)
    assert len(window.context_messages) == len(compact.context_messages) == 2
    assert compact.context_messages[0].content == instruction
    payload = compact.context_messages[1].content
    assert "orphan" not in payload
    snapshot = json.loads(payload[len(STATE_PREFIX) :])
    assert snapshot["workspace_revision"] == 1
    assert snapshot["previous_candidate"] == "Candidate: first patch"
    assert snapshot["remaining_local_budget_tenths"] == 5000
    check_result = snapshot["details"][0]
    assert check_result["status"] == "error"
    assert check_result["command"] == "test_lib.py"
    assert check_result["output"].endswith("AssertionError: current failure")


def test_modes_a_and_b_on_wire(tmp_path, monkeypatch):
    original = tmp_path / "reference"
    bulk = "The following lines are irrelevant to the required result.\n" * 450
    old = "def total():\n    return 0  # initial source\n"
    first = "def total():\n    return 1  # first revision\n"
    fixed = "def total():\n    return 2  # fixed revision\n"
    tests = "from mylib import total\nassert total() == 2, 'expected two'\nprint('test passed')\n"
    add_task(
        original,
        "analysis",
        "data_analysis",
        "Sum the amount in sales.csv and submit the integer.",
        7,
        "exact_int",
        1,
        {"sales.csv": "amount\n3\n4\n"},
    )
    add_task(
        original,
        "coding",
        "coding",
        "Fix mylib.py. Run test_mylib.py after edits and submit DONE.",
        "DONE",
        "unittest",
        3,
        {"mylib.py": old, "test_mylib.py": tests, "notes.txt": bulk},
    )
    add_task(original, "math", "math", "Compute 5 * 7 and submit only the integer.", 35, "exact_int", 2)
    add_task(
        original,
        "search",
        "search",
        "Follow the local references from index.md. Preserve spelling.",
        "Juniper",
        "exact_str",
        4,
        {
            "index.md": "The first key is Orion. Resolve it in route.md.",
            "route.md": "Orion maps to cedar.md. Read the display name.",
            "cedar.md": "Display name: Juniper.",
            "large-one.md": bulk + "end of first irrelevant document",
            "large-two.md": bulk + "end of second irrelevant document",
        },
    )

    scripts = [
        [action("run_python", code="print(sum(map(int, open('sales.csv').read().splitlines()[1:])))")],
        [action("submit_answer", answer="7")],
        [action("read_file", path="mylib.py")],
        [action("write_file", path="./mylib.py", content=first)],
        [action("run_python_file", path="test_mylib.py")],
        [action("read_file", path="mylib.py"), action("list_files", path=".")],
        [action("read_file", path="notes.txt")],
        [action("write_file", path="mylib.py", content=fixed)],
        [action("run_python_file", path="./test_mylib.py")],
        [action("read_file", path="notes.txt")],
        [action("submit_answer", answer="DONE")],
        [action("run_python", code="print(5 * 7)")],
        [action("submit_answer", answer="35")],
        [action("read_file", path="index.md")],
        [action("read_file", path="large-one.md")],
        [action("read_file", path="route.md")],
        [action("read_file", path="large-two.md")],
        [action("write_file", path="index.md", content="forbidden")],
        [action("read_file", path="cedar.md")],
        [action("submit_answer", answer="Juniper")],
    ]
    requests, reports = {}, {}
    for mode in ("A", "B"):
        root = tmp_path / mode
        args = SimpleNamespace(
            run_dir=root,
            archive=None,
            source_dir=original,
            baseline=tmp_path / "A" if mode == "B" else None,
            mode=mode,
            image=None,
            network=None,
            timeout=45,
            proxy_kind="test-double",
            proxy_budget=None,
            local_budget=300_000,
        )
        assert prepare(args) == 0
        for task in (root / "tasks").iterdir():
            assert set(json.loads((task / "task.json").read_text())) == {
                "task_id",
                "task_type",
                "reward",
                "instruction",
            }
            assert not (task / "answer.txt").exists()
        with proxy(
            *[(200, fixture_usage, response(index, calls), None) for index, calls in enumerate(scripts)]
        ) as (url, received):
            monkeypatch.setenv("LLM_BASE_URL", url)
            monkeypatch.setenv("LLM_API_KEY", KEY)
            monkeypatch.setenv("LLM_MODEL", MODEL)
            assert run(args) == 0, (root / "agent-stderr.log").read_text()
        requests[mode] = [received.get_nowait() for _ in scripts]
        assert received.empty()
        assert check(args) == 0
        reports[mode] = json.loads((root / "metrics.json").read_text())
        assert (root / "tasks/coding/workspace/mylib.py").read_text() == fixed
        assert (root / "tasks/coding/workspace/test_mylib.py").read_text() == tests
        assert (root / "tasks/search/workspace/index.md").read_bytes() == (
            original / "search/workspace/index.md"
        ).read_bytes()
        for task, answer in {"analysis": "7", "coding": "DONE", "math": "35", "search": "Juniper"}.items():
            assert (root / "tasks" / task / "answer.txt").read_text() == answer + "\n"

        events, _ = read_jsonl(root / "trace.jsonl")
        traced = [event for event in events if event["event"] == "llm.request"]
        for request, event in zip(requests[mode], traced, strict=True):
            assert_paired(request["messages"])
            assert event["context"]["serialized"] == describe(canonical_json(request["messages"]))
            assert event["tools"]["serialized"] == describe(canonical_json(request["tools"]))
        assert any(
            event.get("status") == "invalid" and event.get("tool") == "write_file"
            for event in events
            if event["event"] == "tool.finished"
        )
        selection = [event for event in events if event["event"] == "context.selected"]
        if mode == "B":
            assert len(selection) == len(traced)
            assert any(event["dropped_exchanges"] > 0 for event in selection)
            assert any(event["stale_exchanges"] > 0 for event in selection)
        else:
            assert not selection

    a, b = requests["A"], requests["B"]
    assert [request["tools"] for request in a] == [request["tools"] for request in b]
    for index in (0, 2, 11, 13):
        assert a[index] == b[index]
    assert all(
        item["function"]["name"] != "write_file"
        for index, request in enumerate(b)
        if not 2 <= index <= 10
        for item in request["tools"]
    )

    # Check exact request sequence around the edit, failed test, reread and fix.
    assert "initial source" in data(a[4]) and "initial source" not in data(b[4])
    assert "first revision" in data(b[6])
    assert any(
        message["role"] == "tool" and "first revision" in message["content"] for message in b[6]["messages"]
    )
    assert "AssertionError: expected two" in data(b[7])
    assert "No Python check recorded after the latest edit" in data(b[8])
    assert "expected two" not in data(b[8])
    assert "test passed" in data(b[10])
    assert "expected two" not in data(b[10])
    assert any(
        item.get("check") == "run_python_file" and item["status"] == "ok"
        for state in states(b[10])
        for item in state["details"]
    )

    # Two oversized results disappear as complete groups. The earlier clues still link to the answer.
    assert bulk in data(a[-1]) and bulk not in data(b[-1])
    assert all(
        clue in data(b[-1]) for clue in ("first key is Orion", "maps to cedar.md", "Display name: Juniper")
    )
    assert states(b[-1])[0]["plan"] == "Plan: trace the references."
    assert states(b[-1])[0]["remaining_local_budget_tenths"] > 0
    assert "initial source" not in data(b[-1]) and "first revision" not in data(b[-1])
    assert "Follow the local references from index.md. Preserve spelling." in data(b[-1])
    assert describe(canonical_json(b[-1]["messages"]))["utf8_bytes"] < WINDOW_BYTES + 1024

    summary_a = json.loads((tmp_path / "A/summary.json").read_text())
    summary_b = json.loads((tmp_path / "B/summary.json").read_text())
    assert summary_a["score"] == summary_b["score"] == 10
    assert summary_a["calls"] == summary_b["calls"] == len(scripts)
    assert summary_a["input_tokens"] > summary_b["input_tokens"]
    context_a, context_b = reports["A"]["context"], reports["B"]["context"]
    assert context_a["first_input_tokens"] == context_b["first_input_tokens"]
    assert (
        context_a["retransmitted_bytes"]["tool_schemas"] == context_b["retransmitted_bytes"]["tool_schemas"]
    )
    assert context_a["retransmitted_bytes"]["messages"] > context_b["retransmitted_bytes"]["messages"]
