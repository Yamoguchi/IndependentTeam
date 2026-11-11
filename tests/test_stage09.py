"""Parallel task sessions through ReAct and a controlled HTTP proxy."""

import asyncio
import json
import os
import signal
import sys
from io import StringIO
from pathlib import Path
from threading import Event

import pytest
from test_budget import INCLUDED, KEY, MODEL, SEPARATE, proxy
from test_task_context import action, add_task, response

from contest_harness.agent.model import TracedModel
from contest_harness.scheduler import run
from contest_harness.telemetry.trace import RunTrace


def events(stream, name):
    return [row for line in stream.getvalue().splitlines() if (row := json.loads(line))["event"] == name]


async def until(predicate):
    async with asyncio.timeout(10):
        while not (result := predicate()):
            await asyncio.sleep(0.01)
        return result


def query(request):
    return next(row["content"] for row in request["messages"] if row["role"] == "user")


def inputs(root, *names):
    for reward, name in enumerate(reversed(names), 1):
        add_task(root, name, "search", f"Read the record for {name}.", name, "exact_str", reward,
                 {"record.md": name})
        path = root / name / "task.json"
        task = json.loads(path.read_text())
        path.write_text(json.dumps({key: task[key] for key in ("task_id", "task_type", "instruction", "reward")}))


async def test_waiting_request_reserves_only_after_settlement():
    gate = Event()
    stream = StringIO()
    trace = RunTrace(stream, limit_tenths=1000)

    async def invoke(model, index):
        with trace.task(index, f"task-{index}", session_id=index, wait_for_budget=True):
            return await model.invoke("parallel", max_tokens=16)

    with proxy((200, INCLUDED, None, gate), (200, SEPARATE, None, None)) as (url, received):
        model = TracedModel(trace, MODEL, url, KEY, timeout=5)
        async with asyncio.TaskGroup() as workers:
            workers.create_task(invoke(model, 1))
            try:
                await asyncio.to_thread(received.get, True, 5)
                workers.create_task(invoke(model, 2))
                waiting, = await until(lambda: events(stream, "llm.waiting_for_budget"))
                assert waiting["session_id"] == 2
                assert received.empty()
                assert trace.in_flight_tenths == waiting["in_flight_tenths"]
            finally:
                gate.set()

    starts = events(stream, "llm.started")
    finishes = events(stream, "llm.finished")
    assert [row["session_id"] for row in starts] == [1, 2]
    assert finishes[0]["sequence"] < starts[1]["sequence"]
    assert finishes[1]["budget_wait_ms"] > 0
    assert all(row["occupied_tenths"] <= 1000 for row in starts)
    assert all(row["budget"]["occupied_tenths"] <= 1000 for row in finishes)
    assert trace.in_flight_tenths == 0
    assert trace.usage.report()["exact_tenths"] == 440
    assert trace.usage.reasoning_tokens == 8
    assert trace.usage.cached_input_tokens == 140


async def test_parallel_run_completes_two_tasks_under_tight_budget(tmp_path):
    inputs(tmp_path, "first", "second")
    gate = Event()
    stream = StringIO()
    trace = RunTrace(stream, limit_tenths=28000)
    with proxy((200, INCLUDED, "first", gate), (200, SEPARATE, "second", None)) as (url, received):
        model = TracedModel(trace, MODEL, url, KEY, timeout=5)
        pending = asyncio.create_task(run(model, MODEL, tmp_path, trace, "B+C", sessions=2))
        try:
            await asyncio.to_thread(received.get, True, 5)
            assert len(events(stream, "llm.started")) == 1
            assert received.empty()
        finally:
            gate.set()
            assert await pending == 0

    assert [row["task_id"] for row in events(stream, "planning.decision")] == ["first", "second"]
    assert all(row["admitted"] for row in events(stream, "planning.decision"))
    first, second = events(stream, "llm.started")
    assert first["estimate"]["tenths"] + second["estimate"]["tenths"] > 28000
    assert second["in_flight_tenths"] == second["estimate"]["tenths"]
    assert trace.budget_report()["remaining_tenths"] == 27560
    assert (tmp_path / "first/answer.txt").read_bytes() == b"first\n"
    assert (tmp_path / "second/answer.txt").read_bytes() == b"second\n"


async def test_session_failure_and_cancellation_preserve_peer(tmp_path):
    inputs(tmp_path, "first", "second", "third", "queued")
    gates = [Event() for _ in range(3)]
    stream = StringIO()
    trace = RunTrace(stream, limit_tenths=100000)

    def answer(request):
        return query(request).split()[-1].rstrip(".")

    with proxy(
        (200, INCLUDED, answer, gates[0]),
        (503, None, None, gates[1]),
        (200, SEPARATE, answer, gates[2]),
        (200, INCLUDED, answer, None),
    ) as (url, received):
        model = TracedModel(trace, MODEL, url, KEY, timeout=5)
        pending = asyncio.create_task(run(model, MODEL, tmp_path, trace, "B+C", sessions=3))
        try:
            order = [answer(await asyncio.to_thread(received.get, True, 5)) for _ in gates]
            sessions = {row["task_id"]: row["session_id"] for row in events(stream, "task.started")}
            worker = next(task for task in asyncio.all_tasks()
                          if task.get_name() == f"harness-session-{sessions[order[0]]}")
            worker.cancel()
            gates[1].set()
            assert answer(await asyncio.to_thread(received.get, True, 5)) == "queued"
            await until(lambda: (tmp_path / "queued/answer.txt").is_file())
            gates[2].set()
            assert await pending == 1
        finally:
            for gate in gates:
                gate.set()
            await pending

    finishes = {row["task_id"]: row for row in events(stream, "task.finished")}
    assert [finishes[name]["status"] for name in order] == ["interrupted", "proxy_error", "submitted"]
    assert {row["status"] for row in events(stream, "llm.finished")} == {"cancelled", "error", "ok"}
    assert trace.in_flight_tenths == 0
    assert trace.usage.held_tenths == sum(
        row["estimate"]["tenths"] for row in events(stream, "llm.finished") if row["status"] != "ok"
    )
    assert trace.usage.measured_tenths == 440
    assert (tmp_path / "queued/answer.txt").read_bytes() == b"queued\n"
    assert (tmp_path / order[2] / "answer.txt").read_text() == order[2] + "\n"
    assert not any((tmp_path / name / "answer.txt").exists() for name in order[:2])


async def test_real_tools_keep_task_state_and_neighbors_isolated(tmp_path):
    inputs(tmp_path, "first", "second")
    neighbor = tmp_path / "second/workspace/record.md"
    (tmp_path / "first/workspace/alias.md").symlink_to(neighbor)
    slow_started = Event()
    stream = StringIO()
    trace = RunTrace(stream, limit_tenths=120000)

    def first_turn(request):
        name = query(request).split()[-1].rstrip(".")
        if name == "second":
            assert slow_started.wait(5)
            return name
        code = (
            "from pathlib import Path\nimport time\n"
            "assert Path('record.md').read_text() == 'first'\n"
            "assert not Path('alias.md').exists()\n"
            f"try:\n Path({str(neighbor)!r}).read_text()\n"
            "except PermissionError:\n print('neighbor denied')\n"
            "else:\n raise AssertionError('neighbor was readable')\n"
            "time.sleep(1.5)\n"
        )
        return response(1, [action("run_python", code=code)])

    def second_turn(request):
        assert query(request) == "Read the record for first."
        assert any("neighbor denied" in message["content"] for message in request["messages"]
                   if message["role"] == "tool")
        return response(2, [action("read_file", path="../second/workspace/record.md")])

    def third_turn(request):
        assert query(request) == "Read the record for first."
        return "first"

    with proxy(
        (200, INCLUDED, first_turn, None), (200, INCLUDED, first_turn, None),
        (200, SEPARATE, second_turn, None), (200, SEPARATE, third_turn, None),
    ) as (url, received):
        model = TracedModel(trace, MODEL, url, KEY, timeout=10)
        pending = asyncio.create_task(run(model, MODEL, tmp_path, trace, "B+C", sessions=2))
        try:
            await until(lambda: events(stream, "tool.started"))
            slow_started.set()
            assert await pending == 0
        finally:
            slow_started.set()
            await pending
    assert received.qsize() == 4
    finished = events(stream, "tool.finished")
    python, = [row for row in finished if row["tool"] == "run_python"]
    rejected, = [row for row in finished if row["tool"] == "read_file"]
    assert python["status"] == "ok" and rejected["status"] == "invalid"
    answers = {row["task_id"]: row for row in events(stream, "answer.saved")}
    assert answers["second"]["sequence"] < python["sequence"]
    assert {row["session_id"] for row in answers.values()} == {1, 2}
    calls = {row["call_id"]: row for row in events(stream, "llm.finished")}
    for row in finished + list(answers.values()):
        assert calls[row["call_id"]]["session_id"] == row["session_id"]
        assert calls[row["call_id"]]["task_index"] == row["task_index"]
    assert neighbor.read_bytes() == b"second"
    assert (tmp_path / "first/answer.txt").read_bytes() == b"first\n"
    assert (tmp_path / "second/answer.txt").read_bytes() == b"second\n"


@pytest.mark.parametrize("interrupt", ["deadline", "sigterm", "sigkill"])
async def test_interruption_leaves_canonical_answer(tmp_path, interrupt):
    root = tmp_path / "tasks"
    inputs(root, "quick", "stuck")
    gate = Event()
    trace = tmp_path / "trace.jsonl"

    def answer(request):
        if query(request) == "Read the record for stuck.":
            gate.wait(10)
            return "stuck"
        return "quick"

    with proxy(*[(200, INCLUDED, answer, None)] * 2) as (url, received):
        process = await asyncio.create_subprocess_exec(
            sys.executable, str(Path(__file__).resolve().parents[1] / "run_agent.py"),
            "--tasks-dir", str(root), "--sessions", "2", "--trace-file", str(trace),
            "--run-timeout", "2" if interrupt == "deadline" else "10",
            env={**os.environ, "LLM_BASE_URL": url, "LLM_API_KEY": KEY, "LLM_MODEL": MODEL},
            stdout=asyncio.subprocess.DEVNULL,
        )
        try:
            await asyncio.to_thread(received.get, True, 10)
            await asyncio.to_thread(received.get, True, 10)
            answer_file = root / "quick/answer.txt"
            await until(answer_file.is_file)
            if interrupt == "sigkill":
                process.kill()
            elif interrupt == "sigterm":
                process.terminate()
            exit_codes = {"deadline": 1, "sigterm": 128 + signal.SIGTERM, "sigkill": -signal.SIGKILL}
            assert await asyncio.wait_for(process.wait(), 5) == exit_codes[interrupt]
            assert answer_file.read_bytes() == b"quick\n"
            assert not (root / "stuck/answer.txt").exists()
            if interrupt != "sigkill":
                finished, = events(StringIO(trace.read_text()), "run.finished")
                assert finished["status"] == ("deadline" if interrupt == "deadline" else "interrupted")
                assert finished["submitted"] == 1
                assert finished["in_flight_tenths"] == 0 and finished["held_tenths"] > 0
        finally:
            gate.set()
            if process.returncode is None:
                process.kill()
                await process.wait()


@pytest.mark.parametrize("sessions", [0, 6])
async def test_rejects_session_counts_outside_contest_range(tmp_path, sessions):
    with pytest.raises(ValueError, match="between 1 and 5"):
        await run(None, MODEL, tmp_path, RunTrace(None), sessions=sessions)
