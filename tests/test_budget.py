"""Stage 02 checks through the actual SDK and a scripted loopback proxy."""

import asyncio
import json
import logging
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from queue import Queue
from threading import Event, Thread
from types import SimpleNamespace

import httpx
import pytest
from openjiuwen.core.common.logging.log_config import configure_log_config

from contest_harness.agent.model import TracedModel
from contest_harness.evaluation.artifacts import read_jsonl
from contest_harness.evaluation.history import report
from contest_harness.evaluation.launch import run
from contest_harness.evaluation.manifest import load_manifest, prepare
from contest_harness.evaluation.metrics import token_rows
from contest_harness.evaluation.scoring import check
from contest_harness.telemetry.trace import RunTrace, error_fields
from contest_harness.telemetry.usage import ProxyUsage, budget_tenths

MODEL = "qwen3.8-27b-noreason"
KEY = "stage02-fixture-credential"
INCLUDED = {
    "prompt_tokens": 100,
    "prompt_tokens_details": {"cached_tokens": 70},
    "completion_tokens": 10,
    "completion_tokens_details": {"reasoning_tokens": 4},
    "total_tokens": 110,
}
SEPARATE = {
    "prompt_tokens": 100,
    "prompt_cache_hit_tokens": 70,
    "prompt_cache_miss_tokens": 30,
    "completion_tokens": 10,
    "reasoning_tokens": 4,
    "total_tokens": 114,
}


@pytest.fixture(autouse=True)
def quiet_sdk():
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    configure_log_config({"output": [], "interface_output": [], "performance_output": []})
    yield
    logging.disable(previous)


@contextmanager
def proxy(*responses):
    """Queue (status, usage, answer, gate) for each real HTTP request."""
    received = Queue()
    script = iter(responses)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            status, usage, answer, gate = next(script, (500, None, None, None))
            received.put(request)
            if gate is not None:
                gate.wait(10)
            payload = {"error": {"message": "controlled failure"}}
            if status == 200:
                answer = answer(request) if callable(answer) else answer
                message = answer if isinstance(answer, dict) else {"role": "assistant", "content": "ok"}
                if isinstance(answer, str):
                    message["tool_calls"] = [
                        {
                            "id": "submit",
                            "type": "function",
                            "function": {
                                "name": "submit_answer",
                                "arguments": json.dumps({"answer": answer}),
                            },
                        }
                    ]
                finish = message.get("_finish_reason", "tool_calls" if message.get("tool_calls") else "stop")
                payload = {
                    "id": "fixture",
                    "object": "chat.completion",
                    "created": 0,
                    "model": request["model"],
                    "choices": [
                        {"index": 0, "finish_reason": finish,
                         "message": {key: value for key, value in message.items() if key != "_finish_reason"}}
                    ],
                }
                if usage is not None:
                    payload["usage"] = usage(request) if callable(usage) else usage
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        worker = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        worker.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", received
        finally:
            server.shutdown()
            worker.join()


def rows(stream):
    return token_rows([json.loads(line) for line in stream.getvalue().splitlines()])


def test_budget_decimal_and_usage_formats():
    assert budget_tenths("100.1") == 1001
    assert budget_tenths("0") == 0
    for invalid in ("-1", "1.01", "nan", "1e3"):
        with pytest.raises(ValueError):
            budget_tenths(invalid)
    assert ProxyUsage.read(INCLUDED).billable_output_tokens == 10
    assert ProxyUsage.read(SEPARATE).billable_output_tokens == 14
    assert ProxyUsage.read({**SEPARATE, "prompt_tokens": 30}).input_tokens == 100
    assert (
        ProxyUsage.read(
            {"prompt_tokens": 100, "completion_tokens": 10, "reasoning_tokens": 4}
        ).billable_output_tokens
        is None
    )
    assert ProxyUsage.read({"completion_tokens": 0}).billable_output_tokens == 0
    assert ProxyUsage.read({}).input_tokens is None


async def test_parallel_reservations_are_atomic():
    gate = Event()
    stream = StringIO()
    trace = RunTrace(stream, limit_tenths=1000)
    with proxy((200, INCLUDED, None, gate), (200, SEPARATE, None, None)) as (url, received):
        model = TracedModel(trace, MODEL, url, KEY)

        async def invoke(index):
            with trace.task(index, f"task-{index}"):
                return await model.invoke("parallel", max_tokens=16)

        first = asyncio.create_task(invoke(1))
        try:
            request = await asyncio.to_thread(received.get, True, 5)
            assert request["max_tokens"] == 16
            reserved = trace.in_flight_tenths
            assert reserved <= 1000 < reserved * 2
            with pytest.raises(Exception) as raised:
                await invoke(2)
            assert error_fields(raised.value)["error_type"] == "BudgetExceeded"
            assert trace.in_flight_tenths == reserved
            assert received.empty()
        finally:
            gate.set()
            await first
        await invoke(3)
        assert received.qsize() == 1
    assert trace.in_flight_tenths == 0
    assert trace.usage.report()["exact_tenths"] == 440
    assert trace.usage.cached_input_tokens == 140
    assert trace.usage.reasoning_tokens == 8
    assert [(r["task_index"], r["status"]) for r in rows(stream)] == [
        (1, "ok"),
        (2, "budget_denied"),
        (3, "ok"),
    ]


@pytest.mark.parametrize("outcome", ["cancelled", "timeout", "no_usage", "partial_usage", "partial_cache"])
async def test_possible_dispatch_keeps_unmeasured_reserve(outcome):
    gate = Event() if outcome in {"cancelled", "timeout"} else None
    usage = {
        "partial_usage": {"prompt_tokens": 100},
        "partial_cache": {"prompt_tokens_details": {"cached_tokens": 70}},
    }.get(outcome)
    stream = StringIO()
    trace = RunTrace(stream)
    with proxy((200, usage, None, gate)) as (url, received):
        model = TracedModel(trace, MODEL, url, KEY, timeout=0.3 if outcome == "timeout" else 5)
        try:
            if outcome == "cancelled":
                pending = asyncio.create_task(model.invoke("cancel", max_tokens=16))
                await asyncio.to_thread(received.get, True, 5)
                pending.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await pending
            elif outcome == "timeout":
                with pytest.raises(TimeoutError):
                    await model.invoke("timeout", max_tokens=16)
                assert received.qsize() == 1
            else:
                await model.invoke("missing usage", max_tokens=16)
        finally:
            if gate is not None:
                gate.set()
    (row,) = rows(stream)
    assert row["status"] == (outcome if gate else "ok")
    assert row["usage_status"] == "unknown"
    assert row["exact_tenths"] is None
    assert row["measured_tenths"] == {"partial_usage": 100, "partial_cache": 70}.get(outcome, 0)
    assert row["held_tenths"] == (
        160 if outcome == "partial_usage" else row["estimate"]["tenths"] - row["measured_tenths"]
    )
    assert trace.usage.held_tenths == row["held_tenths"]
    assert trace.in_flight_tenths == 0


async def test_connect_failure_releases_reservation(monkeypatch):
    stream = StringIO()
    trace = RunTrace(stream, limit_tenths=1000)
    attempts = []

    async def refuse(self, request):
        attempts.append(trace.in_flight_tenths)
        raise httpx.ConnectError("fixture refused before writing", request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", refuse)
    model = TracedModel(trace, MODEL, "http://127.0.0.1:1/v1", KEY)
    with pytest.raises(Exception) as raised:
        await model.invoke("refused", max_tokens=16)
    assert error_fields(raised.value)["error_type"] == "ConnectError"
    (row,) = rows(stream)
    assert row["status"] == "not_sent"
    assert attempts == [row["estimate"]["tenths"]]
    assert row["calls"] == row["held_tenths"] == row["measured_tenths"] == 0
    assert trace.budget_report()["remaining_tenths"] == 1000


async def test_wire_estimate_uses_effective_model_messages_and_tools():
    stream = StringIO()
    trace = RunTrace(stream)
    with proxy((200, SEPARATE, None, None), (200, SEPARATE, None, None)) as (url, received):
        model = TracedModel(trace, MODEL, url, KEY)
        await model.invoke("кратко", model="qwen3.6-35b-a3b")
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "tool",
                    "description": "Схема инструмента",
                    "parameters": {"type": "object", "properties": {"arg": {"type": "string"}}},
                },
            }
        ]
        await model.invoke("кратко", model="qwen3.6-35b-a3b", tools=tools)
        requests = [received.get_nowait(), received.get_nowait()]
    first, second = rows(stream)
    assert all(request["max_tokens"] == 4096 for request in requests)
    assert second["estimate"]["reasoning_margin"] == 4096
    assert second["estimate"]["input_tokens"] > first["estimate"]["input_tokens"]
    assert trace.usage.report()["exact_tenths"] == 480


async def test_underestimate_is_reconciled_and_blocks_further_calls():
    stream = StringIO()
    trace = RunTrace(stream, limit_tenths=1000)
    over = {"prompt_tokens": 1300, "completion_tokens": 17, "total_tokens": 1317}
    with proxy((200, over, None, None)) as (url, received):
        model = TracedModel(trace, MODEL, url, KEY)
        await model.invoke("exceeded", max_tokens=16)
        with pytest.raises(Exception) as raised:
            await model.invoke("exceeded", max_tokens=16)
        assert error_fields(raised.value)["error_type"] == "BudgetExceeded"
        assert received.qsize() == 1
    row = rows(stream)[0]
    assert row["estimate_overruns"] == 1
    assert row["estimate_overrun_tenths"] == 1310 - row["estimate"]["input_tokens"]
    assert trace.usage.report()["exact_tenths"] == 1470
    assert trace.budget_report()["local_overrun_tenths"] == 470


@pytest.mark.parametrize("mode", ["complete", "failure", "interrupted", "denied"])
def test_benchmark_budget_and_recovery(tmp_path, monkeypatch, mode):
    original = tmp_path / "original"
    for index, (left, right, answer) in enumerate(((7, 11, 18), (8, 13, 21)), 1):
        task = original / f"task-{index}"
        task.mkdir(parents=True)
        (task / "task.json").write_text(
            json.dumps(
                {
                    "task_id": f"local-{index}",
                    "task_type": "math",
                    "instruction": f"Compute {left} + {right}.",
                    "reward": index,
                    "expected": answer,
                    "check": "exact_int",
                }
            )
        )
    args = SimpleNamespace(
        source_dir=original,
        archive=None,
        run_dir=tmp_path / "run",
        baseline=None,
        image=None,
        network=None,
        timeout=4 if mode == "interrupted" else 20,
        proxy_kind="test-double",
        proxy_budget=None,
        local_budget=0 if mode == "denied" else 24_000,
    )
    prepare(args)
    gate = Event() if mode == "interrupted" else None
    code = 503 if mode == "failure" else 200
    with proxy((200, INCLUDED, "18", None), (code, SEPARATE if code == 200 else None, "21", gate)) as (
        url,
        received,
    ):
        monkeypatch.setenv("LLM_BASE_URL", url)
        monkeypatch.setenv("LLM_API_KEY", KEY)
        monkeypatch.setenv("LLM_MODEL", MODEL)
        try:
            assert run(args) == {"complete": 0, "failure": 1, "interrupted": 124, "denied": 1}[mode]
        finally:
            if gate:
                gate.set()
        assert received.qsize() == (0 if mode == "denied" else 2)
    first = args.run_dir / "tasks" / "task-1" / "answer.txt"
    if mode == "denied":
        assert not first.exists()
    else:
        assert first.read_bytes() == b"18\n"
    assert check(args) == (0 if mode == "complete" else 1)
    summary = json.loads((args.run_dir / "summary.json").read_text())
    calls, _ = read_jsonl(args.run_dir / "tokens.jsonl")
    for field in (
        "calls",
        "measured_calls",
        "measured_tenths",
        "held_tenths",
        "denied_calls",
        "estimate_overruns",
    ):
        assert summary[field] == sum(row[field] for row in calls)
    manifest = load_manifest(args.run_dir)
    if mode in {"failure", "interrupted"}:
        assert summary["output_equivalent"] is None
        assert summary["usage_status"] == "unknown"
        assert summary["measured_tenths"] == 200
        assert summary["unknown_usage_calls"] == 1
        assert summary["held_tenths"] == calls[1]["estimate"]["tenths"]
        assert summary["correct"] == summary["submitted"] == 1
    if mode == "complete":
        runtime_files = manifest["runtime"]["code_sha256"]
        assert {
            "run_agent.py",
            "contest_harness/cli.py",
            "contest_harness/agent/react.py",
            "contest_harness/telemetry/ledger.py",
            "contest_harness/workspace/sandbox.py",
        } <= runtime_files.keys()
        assert all("/evaluation/" not in name for name in runtime_files)
        assert manifest["runtime_code_matches_recorded_files"]
        assert summary["exact_tenths"] == 440
        assert summary["correct"] == summary["submitted"] == 2
    if mode == "denied":
        assert summary["exact_tenths"] == 0
        assert summary["denied_calls"] == 2
    if mode == "interrupted":
        assert not manifest["journal"]["finished_event"]
        assert calls[1]["status"] == "unfinished"
        metrics = json.loads((args.run_dir / "metrics.json").read_text())
        assert metrics["budget"]["recovered_in_flight_tenths"] == summary["held_tenths"]
        recovered = tmp_path / "recovered"
        report(SimpleNamespace(source_run=args.run_dir, run_dir=recovered, baseline=None))
        assert json.loads((recovered / "summary.json").read_text()) == summary
    else:
        assert manifest["journal"]["usage_totals_match_stdout"]
        assert manifest["journal"]["usage_totals_match_finish"]
    assert KEY not in (args.run_dir / "trace.jsonl").read_text()
