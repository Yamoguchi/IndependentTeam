"""Exercise compact tool output and bounded generation through the actual agent."""

import json
from io import StringIO
from math import ceil
from pathlib import Path
from types import SimpleNamespace

import pytest
from openjiuwen.core.foundation.llm import UserMessage
from test_budget import INCLUDED, KEY, MODEL, proxy
from test_task_context import action, add_task, assert_paired, response

from contest_harness.agent.generation import GenerationFailed, GenerationPolicy, profile_limits
from contest_harness.agent.model import TracedModel
from contest_harness.evaluation.artifacts import read_jsonl
from contest_harness.evaluation.launch import run
from contest_harness.evaluation.manifest import prepare
from contest_harness.evaluation.scoring import check
from contest_harness.json_codec import canonical_json
from contest_harness.telemetry.trace import RunTrace
from contest_harness.workspace.output import STORE_BYTES, ToolOutputs, read_text
from contest_harness.workspace.output import test_summary as parse_test_summary
from contest_harness.workspace.sandbox import EXTENDED_OUTPUT_LIMIT, run_python


def test_extended_output_and_retention(tmp_path):
    exact = "x" * (64 * 1024 - 1) + "ё"
    assert run_python(tmp_path, f"print({exact!r})", extended=True) == f"Exit code 0\n{exact}\n"
    out = run_python(tmp_path, "print('line repeated\\n' * 10000); print('tail retained')", extended=True)
    assert out.startswith("Exit code 0\n")
    assert out.endswith("tail retained\n")
    assert out.count("line repeated") == 10000
    overflow = run_python(tmp_path, f"print('x' * {EXTENDED_OUTPUT_LIMIT + 1})", extended=True)
    assert overflow.startswith(f"Python exceeded the {EXTENDED_OUTPUT_LIMIT}-byte output limit.")

    large_file = tmp_path / "large.txt"
    large_file.write_text("Начало\n" + ("x" * 75_000) + "\nКонец\n")
    source = read_text(large_file)
    assert source.startswith("Начало") and source.endswith("Конец\n")
    outputs = ToolOutputs(RunTrace(None))
    try:
        assert outputs.root.is_relative_to(Path("/tmp"))
        summary = outputs.compact("run_python", out)
        assert summary.count("line repeated") == 1
        assert "tail retained" in summary
        assert "repeated output" in outputs.compact("run_python", out)
        assert "tail retained" in outputs.details("out-1", "diagnostic")
        assert "line repeated" in outputs.details("out-1", "0:100")
        assert sum(file.stat().st_size for file in outputs.root.iterdir()) <= EXTENDED_OUTPUT_LIMIT
        unknown = outputs.compact("run_python_file", "Exit code 1\nCustom runner: regression in step 4\n")
        assert "Result: failed (test counts unknown)" in unknown
        assert "regression in step 4" in outputs.details("out-2", "diagnostic")
        for index in range(12):
            outputs.compact("read_file", f"{index}: {source}")
        assert sum(file.stat().st_size for file in outputs.root.iterdir()) <= STORE_BYTES
        assert "line repeated" in outputs.details("out-1", "80000:80100")
        with pytest.raises(ValueError, match="Unknown or expired"):
            outputs.details("../task.json", "0:100")
        with pytest.raises(ValueError, match="up to 4096"):
            outputs.details(f"out-{outputs.sequence}", "0:5000")
    finally:
        outputs.close()
    assert not outputs.root.exists()


def test_test_summary_is_conservative():
    assert parse_test_summary("Ran 3 tests in 0.001s\n\nOK (skipped=1)", 0) == (2, 0, 3)
    assert parse_test_summary("===== 1 failed, 3 passed, 1 skipped in 0.15s =====", 1) == (3, 1, 5)
    assert parse_test_summary("===== 3 passed, 1 error in 0.15s =====", 1) is None
    assert parse_test_summary("FAILED (failures=1)", 1) is None
    assert parse_test_summary("Ran 2 tests in 0.000s\n\nOK", 1) is None
    assert parse_test_summary(
        "FAIL: test_many (__main__.Check.test_many) (x=1)\n"
        "Ran 2 tests in 0.000s\n\nFAILED (failures=1)", 1,
    ) is None


def complete(index, name, **arguments):
    return response(index, [action(name, **arguments)])


def synthetic_usage(message):
    completion = ceil(len(canonical_json(message).encode()) / 4)

    def usage(request):
        prompt = ceil(len(canonical_json(request).encode()) / 4)
        output = min(completion, request["max_tokens"])
        return {"prompt_tokens": prompt, "completion_tokens": output, "total_tokens": prompt + output}

    return usage


def test_modes_b_c_d_on_wire(tmp_path, monkeypatch):
    original = tmp_path / "reference"
    first = "def total():\n    return 1\n"
    long_patch = "# " + "structured output requires a sufficient limit. " * 100 + "\ndef total():\n    return 2\n"
    tests = """import unittest
from mylib import total

class Check(unittest.TestCase):
    def test_total(self):
        self.assertEqual(total(), 2, "expected two")

    def test_type(self):
        self.assertIsInstance(total(), int)

unittest.main()
"""
    add_task(original, "coding", "coding", "Repair mylib.py and run the original tests before DONE.",
             "DONE", "unittest", 4, {"mylib.py": "def total():\n    return 0\n", "test_mylib.py": tests})
    add_task(original, "python", "data_analysis", "Run the Python calculation and return its final integer.",
             35, "exact_int", 2, {"notes.txt": "input\n"})
    files = {f"item-{index:04d}.md": "An ordinary record.\n" for index in range(850)}
    files["item-0300.md"] = "Display name: Juniper.\n"
    files["guide.md"] = "x" * 10_000 + "\nThe target has a Display name entry.\n" + "y" * 10_000
    add_task(original, "search", "search", "Find the Display name in the matching record and submit it.",
             "Juniper", "exact_str", 3, files)

    reports = {}
    ref = None
    for mode in ("B", "B+C", "B+C+D"):
        compact = mode != "B"
        limits = ["short=96", "analysis=410", "structured=270", "code=1800", "debug=500"] if mode.endswith("+D") else []
        messages = [
            complete(0, "read_file", path="mylib.py"),
            complete(1, "write_file", path="mylib.py", content=first),
            complete(2, "run_python_file", path="test_mylib.py"),
        ]
        patch = complete(4, "write_file", path="mylib.py", content=long_patch)
        cut = response(3, [action("submit_answer", answer="BAD"), action("write_file", path="mylib.py", content="")])
        cut["_finish_reason"] = "length"
        cut["tool_calls"][1]["function"]["arguments"] = '{"path":"mylib.py","content":"# cut'
        if limits:
            messages.append(lambda request: cut if request["max_tokens"] < 1400 else patch)
        messages += [
            patch,
            complete(5, "run_python_file", path="test_mylib.py"),
            complete(6, "submit_answer", answer="DONE"),
            complete(7, "run_python", code="print('noise repeated\\n' * 900); print(5 * 7)"),
        ]
        if compact:
            messages.append(complete(8, "output_details", id="out-1", range="diagnostic"))
        messages += [
            complete(9, "submit_answer", answer="35"),
            complete(10, "list_files", path="."),
            complete(11, "list_files", path="."),
            complete(12, "read_file", path="guide.md"),
        ]
        if compact:
            messages.extend([
                complete(13, "output_details", id="out-1", range="3650:4000"),
                complete(14, "search_text", path=".", query="Display name:"),
            ])
        else:
            messages.append(complete(14, "run_python", code=(
                "from pathlib import Path\nfor p in Path('.').glob('*.md'):\n"
                " for line in p.read_text().splitlines():\n  if 'Display name:' in line: print(line)"
            )))
        messages.append(complete(15, "submit_answer", answer="Juniper"))

        args = SimpleNamespace(
            run_dir=tmp_path / mode, source_dir=original, archive=None, baseline=ref,
            mode=mode, generation_limit=limits, image=None, network=None, timeout=60,
            proxy_kind="test-double", proxy_budget=None, local_budget=300_000,
        )
        assert prepare(args) == 0
        with proxy(*[
            (200, synthetic_usage(cut if callable(message) else message), message, None) for message in messages
        ]) as (url, received):
            monkeypatch.setenv("LLM_BASE_URL", url)
            monkeypatch.setenv("LLM_API_KEY", KEY)
            monkeypatch.setenv("LLM_MODEL", MODEL)
            assert run(args) == 0, (args.run_dir / "agent-stdout.jsonl").read_text()
        requests = [received.get_nowait() for _ in messages]
        assert received.empty()
        for request in requests:
            assert_paired(request["messages"])
            assert "response_format" not in request
        if limits:
            assert [request["max_tokens"] for request in requests[:7]] == [1800, 1800, 270, 500, 1800, 270, 96]
            retry = canonical_json(requests[4]["messages"])
            assert "previous completion could not be used (length)" in retry and '"BAD"' not in retry
        assert check(args) == 0
        ref = args.run_dir
        assert (ref / "tasks/coding/workspace/mylib.py").read_text() == long_patch
        assert (ref / "tasks/coding/workspace/test_mylib.py").read_text() == tests
        assert (ref / "tasks/coding/answer.txt").read_text() == "DONE\n"
        if compact:
            failure_wire = canonical_json(requests[3]["messages"])
            assert all(value in failure_wire for value in (
                "Exit code 1", "1 passed, 1 failed", "AssertionError: 1 != 2 : expected two",
            ))
            assert "2 passed, 0 failed" in canonical_json(requests[6 if limits else 5]["messages"])
            assert any("item-0300.md" in canonical_json(request) for request in requests[-2:])
            assert any("out-1 bytes 3650:4000" in canonical_json(request) for request in requests[-2:])
            assert any("repeated output" in canonical_json(request) for request in requests)
        events, _ = read_jsonl(ref / "trace.jsonl")
        assert sum(event.get("tool") == "submit_answer" for event in events if event["event"] == "tool.started") == 3
        reports[mode] = json.loads((ref / "summary.json").read_text())

    b, c, d = (reports[mode] for mode in ("B", "B+C", "B+C+D"))
    assert b["score"] == c["score"] == d["score"] == 9
    assert c["tool_output_bytes"] < b["tool_output_bytes"]
    assert c["detail_requests"] == d["detail_requests"] == 2
    assert b["generation_truncations"] == c["generation_truncations"] == 0
    assert d["generation_truncations"] == d["generation_recoveries"] == 1
    assert d["calls"] == c["calls"] + 1


@pytest.mark.parametrize("budget", [None, 1000])
async def test_generation_recovery_stops_or_respects_shared_budget(budget):
    stream = StringIO()
    trace = RunTrace(stream, limit_tenths=budget * 10 if budget is not None else None)
    malformed = complete(0, "submit_answer", answer="OK")
    malformed["tool_calls"][0]["function"]["arguments"] = '{"answer":"OK","answer":"bad"}'
    responses = [(200, INCLUDED, malformed, None)] * 3
    policy = GenerationPolicy("math", {"submit_answer": {"answer"}}, profile_limits([], 2048))
    with proxy(*responses) as (url, received):
        model = TracedModel(trace, MODEL, url, KEY)
        with model.generation(policy):
            if budget is None:
                with pytest.raises(GenerationFailed, match="malformed_arguments"):
                    await model.invoke([UserMessage(content="test")])
            else:
                with pytest.raises(Exception, match="Insufficient local budget"):
                    await model.invoke([UserMessage(content="test")])
    assert received.qsize() == (2 if budget is None else 1)
    assert trace.usage.denied_calls == int(budget is not None)
    assert trace.usage.calls == received.qsize()


def test_unrecoverable_completion_stops_only_its_task(tmp_path, monkeypatch):
    original = tmp_path / "reference"
    add_task(original, "first", "math", "Return the integer 1.", 1, "exact_int", 2)
    add_task(original, "second", "math", "Return the integer 2.", 2, "exact_int", 2)
    args = SimpleNamespace(
        run_dir=tmp_path / "run", source_dir=original, archive=None, baseline=None,
        mode="B+C+D", image=None, network=None, timeout=60, proxy_kind="test-double",
        proxy_budget=None, local_budget=50_000,
    )
    invalid = complete(0, "submit_answer", answer="1")
    invalid["_finish_reason"] = "length"  # The visible answer looks complete.
    assert prepare(args) == 0
    with proxy(
        (200, INCLUDED, invalid, None), (200, INCLUDED, invalid, None),
        (200, INCLUDED, complete(1, "submit_answer", answer="2"), None),
    ) as (url, received):
        monkeypatch.setenv("LLM_BASE_URL", url)
        monkeypatch.setenv("LLM_API_KEY", KEY)
        monkeypatch.setenv("LLM_MODEL", MODEL)
        assert run(args) == 1
    assert received.qsize() == 3
    events, _ = read_jsonl(args.run_dir / "agent-stdout.jsonl")
    assert [item["status"] for item in events if item["event"] == "task"] == ["generation_failed", "submitted"]
    assert not (args.run_dir / "tasks/first/answer.txt").exists()
    assert (args.run_dir / "tasks/second/answer.txt").read_text() == "2\n"
