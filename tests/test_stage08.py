"""Sequential planning through the real ReAct SDK and a scripted HTTP proxy."""

import json
from io import StringIO
from types import SimpleNamespace

import pytest
from test_budget import KEY, MODEL, proxy
from test_task_context import add_task

from contest_harness.evaluation.artifacts import read_jsonl, write_jsonl
from contest_harness.evaluation.history import planning_history
from contest_harness.evaluation.launch import run
from contest_harness.evaluation.manifest import prepare
from contest_harness.evaluation.scoring import check
from contest_harness.planning.forecast import PlanningHistory, estimate_task, settings_for
from contest_harness.planning.routing import ModelEvidence, Route, TaskRouter
from contest_harness.scheduler import run as run_agent
from contest_harness.telemetry.trace import RunTrace, describe
from contest_harness.telemetry.usage import CallEstimate

SETTINGS = settings_for("B+C", None, False, 20, None)


def observation(kind, index, cost, **overrides):
    return {
        "schema_version": 1, "source_run_id": "fixture-history", "proxy_kind": "test-double",
        "task_id": f"historical-{kind}-{index}", "instruction_sha256": describe(f"{kind} {index}")["sha256"],
        "task_type": kind, "route": "BASELINE", "model": MODEL, "smart_model": None,
        "settings": SETTINGS, "exact_tenths": cost, "local_ms": 25, "passed": True, **overrides,
    }


def test_forecast_groups_and_independent_probability():
    rows = [observation("search", index, cost) for index, cost in enumerate([100, 300, 800])]
    rows += [
        observation("search", 20, 10, model="other-model"),
        observation("search", 21, 1, task_id="eval-task"),
        observation("search", 22, 1, instruction_sha256="eval-instruction"),
        observation("search", 0, 1),
    ]
    history = PlanningHistory(rows).independent_of({"eval-task"}, {"eval-instruction"})
    route = Route("BASELINE", MODEL, "control", {"files_seen": 2})
    forecast = estimate_task("search", route, SETTINGS, history, first_reserve=700)
    assert forecast["cost_samples"] == 3 and forecast["estimated_tenths"] == 300
    assert forecast["admission_tenths"] == 700 and forecast["estimated_local_ms"] == 25
    assert forecast["success_probability"] is None

    verified = [
        observation("search", index, 1000, proxy_kind="organizer", passed=index < 15)
        for index in range(20)
    ]
    forecast = estimate_task("search", route, SETTINGS, PlanningHistory(verified))
    assert forecast["success_probability"] == 16 / 22

    smart = ModelEvidence("qwen3.6-35b-a3b-noreason", 5, 4, 500, 3, 2)
    cascade = Route("CASCADE", MODEL, "test", {"files_seen": 2}, smart=smart)
    settings = settings_for("B+C+D", {"code": 1536}, False, 8, 0.001)
    forecast = estimate_task("coding", cascade, settings, history, input_bytes=1500)
    components = forecast["fallback_components_tenths"]
    assert all(value > 0 for value in components.values())
    assert forecast["estimated_tenths"] == sum(components.values())


@pytest.mark.parametrize("mixed", [False, True], ids=["two", "mixed"])
def test_planner_ablation_and_skip(tmp_path, monkeypatch, mixed):
    source = tmp_path / "reference"
    if mixed:
        add_task(source, "01-expensive", "math", "Determine the integer from incomplete measurements.",
                 11, "exact_int", 240)
    add_task(source, "02-low", "search", "Read the registry in low.md and find the city.",
             "Rome", "exact_str", 3, {"low.md": "City: Rome"})
    add_task(source, "03-high", "search", "Read the registry in high.md and find the city.",
             "Lyon", "exact_str", 7, {"high.md": "City: Lyon"})
    history = tmp_path / "history.jsonl"
    write_jsonl(history, [
        *(observation("math", index, 60000) for index in range(3)),
        *(observation("search", index, cost) for index, cost in enumerate([2000, 3000, 4000])),
    ])
    replies = {
        "01-expensive": (200, {"prompt_tokens": 10000, "completion_tokens": 200}, {"role": "assistant", "content": ""}, None),
        "02-low": (200, {"prompt_tokens": 800, "completion_tokens": 20}, "Rome", None),
        "03-high": (200, {"prompt_tokens": 3000, "completion_tokens": 100}, "Lyon", None),
    }
    reference = None
    for mode in ("control", "F"):
        directory = tmp_path / mode
        expected_order = (
            ["01-expensive"] if mixed and mode == "control" else
            ["03-high", "02-low"] if mode == "F" else ["02-low", "03-high"]
        )
        args = SimpleNamespace(
            run_dir=directory, archive=None, source_dir=source, baseline=reference, mode="B+C",
            image=None, network=None, timeout=45, proxy_kind="test-double", proxy_budget=None,
            local_budget=32000, planner=mode, planning_history=history,
        )
        assert prepare(args) == 0
        assert all("expected" not in item.read_text() and "check" not in json.loads(item.read_text())
                   for item in (directory / "tasks").glob("*/task.json"))
        with proxy(*(replies[name] for name in expected_order)) as (url, received):
            monkeypatch.setenv("LLM_BASE_URL", url)
            monkeypatch.setenv("LLM_API_KEY", KEY)
            monkeypatch.setenv("LLM_MODEL", MODEL)
            assert run(args) == int(mixed)
        assert received.qsize() == len(expected_order)
        assert check(args) == int(mixed)
        metrics = json.loads((directory / "metrics.json").read_text())
        rows, _ = read_jsonl(directory / "results-by-task.jsonl")
        events, _ = read_jsonl(directory / "trace.jsonl")
        assert max(row["sequence"] for row in events if row["event"] == "planning.task") < next(
            row["sequence"] for row in events if row["event"] == "task.started"
        )
        assert all(row["success_probability"] is None for row in rows)
        if mode == "F":
            assert metrics["execution_order"] == expected_order
            assert metrics["score"] == 10 and metrics["output_equivalent"] == 500
            assert metrics["budget"]["remaining_tenths"] == 27000
            assert metrics["planning"]["known_positive_costs"] == {
                "samples": 2, "mae": 150.0, "mape_samples": 2, "mape_percent": 112.5,
            }
            assert metrics["planning"]["cost_overruns"] == 1
            assert (directory / "tasks/03-high/answer.txt").read_bytes() == b"Lyon\n"
            if mixed:
                assert metrics["planning"]["planned_order"] == ["01-expensive", *expected_order]
                assert metrics["planning"]["skipped"] == {"insufficient_task_budget": 1}
                assert rows[0]["stop_reason"] == "insufficient_task_budget" and rows[0]["actual_cost"] is None
                assert not (directory / "tasks/01-expensive/answer.txt").exists()
            exported = tmp_path / "exported.jsonl"
            assert planning_history(SimpleNamespace(run_dirs=[directory], output=exported)) == 0
            samples, _ = read_jsonl(exported)
            assert {row["task_id"]: row["exact_tenths"] for row in samples} == {"02-low": 1000, "03-high": 4000}
        elif mixed:
            assert metrics["score"] == 0
            assert metrics["planning"]["admitted_with_budget_denial"] == 2
        else:
            assert metrics["score"] == 10 and metrics["execution_order"] == expected_order
        reference = directory


async def test_admission_counts_measured_held_and_in_flight(tmp_path):
    add_task(tmp_path, "math", "math", "Compute a rounded result.", 1, "exact_int", 1)
    stream = StringIO()
    trace = RunTrace(stream, limit_tenths=32000)
    trace.usage.input_tokens = 2000
    trace.usage.held_tenths = 3000
    assert trace.ledger.reserve(CallEstimate(input_tokens=5000, output_limit=0, reasoning_margin=0))
    assert await run_agent(None, MODEL, tmp_path, trace, "B+C", planner="F") == 1
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    decision = next(row for row in events if row["event"] == "planning.decision")
    assert decision["remaining_tenths"] == 22000
    assert decision["admission_tenths"] > 22000 and decision["admission_tenths"] <= 32000
    assert decision["reason"] == "insufficient_task_budget"
    assert not any(row["event"] == "task.started" for row in events)


async def test_local_route_can_publish_with_zero_token_budget(tmp_path):
    add_task(tmp_path, "math", "math", "Compute 7 * 6. Return only the integer.", 42, "exact_int", 3)
    stream = StringIO()
    trace = RunTrace(stream, limit_tenths=0)
    router = TaskRouter(MODEL, {}, False)
    assert await run_agent(None, MODEL, tmp_path, trace, router=router, planner="F") == 0
    events = [json.loads(line) for line in stream.getvalue().splitlines()]
    plan = next(row for row in events if row["event"] == "planning.task")
    assert plan["route"] == "LOCAL" and plan["reference_method"] == "integer_expression"
    assert plan["forecast"]["estimated_tenths"] == 0 and plan["priority"] == 30
    assert not trace.usage.calls
    assert (tmp_path / "math/answer.txt").read_bytes() == b"42\n"
