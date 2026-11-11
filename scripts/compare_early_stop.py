"""Prepare, run and independently score held-out tasks for the F/G comparison."""

import argparse
import json
import os
import sys
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from contest_harness.evaluation.artifacts import read_jsonl, write_jsonl, write_text  # noqa: E402
from contest_harness.evaluation.launch import run  # noqa: E402
from contest_harness.evaluation.manifest import load_manifest, prepare  # noqa: E402
from contest_harness.evaluation.report import generate  # noqa: E402
from contest_harness.telemetry.usage import budget_tenths  # noqa: E402


@dataclass(frozen=True)
class Case:
    key: str
    kind: str
    reward: int
    instruction: str
    files: dict[str, str]
    answer: str | None


# These families are held out from tests/test_stage10.py, which exercises
# the evidence gate and retry thresholds using separate cases.
CASES = (
    Case("depot-route", "search", 7,
         "Find the minimum total cost from Arbor to Elm. All links in `links.md` are directed. "
         "A route must pass through Cedar. Return only an integer.",
         {"links.md": "Arbor -> Birch 3\nArbor -> Cedar 9\nBirch -> Cedar 2\n"
                      "Birch -> Elm 1\nCedar -> Dogwood 2\nCedar -> Elm 8\nDogwood -> Elm 1\n"}, "8"),
    Case("relay-owner", "search", 4,
         "Use `routes.md` and `people.md` to identify the backup lead for the southern relay. Return only the name.",
         {"routes.md": "Northern relay: primary R2, backup R1.\nSouthern relay: primary R1, backup R3.\n",
          "people.md": "R1: Mira Jans.\nR2: Dee Wong.\nR3: Sami Noor.\n"}, "Sami Noor"),
    Case("rolling-margin", "data_analysis", 7,
         "Join `shipments.csv` with `returns.csv` on shipment ID. For Q2 shipments only, "
         "sum (shipped units minus returned units) times unit margin. Unlisted returns count as zero. "
         "Return only an integer.",
         {"shipments.csv": "id,quarter,units,margin\nJ1,Q2,11,7\nJ2,Q2,8,4\nJ3,Q1,40,9\nJ4,Q2,3,10\n",
          "returns.csv": "id,units\nJ1,3\nJ3,1\nJ4,2\n"}, "98"),
    Case("daily-peak", "data_analysis", 4,
         "From `meter.csv`, compute the total kWh on each day across both meters. "
         "Return the median daily total as an integer.",
         {"meter.csv": "day,meter,kWh\n2025-01-01,A,7\n2025-01-01,B,3\n2025-01-02,A,12\n"
                       "2025-01-02,B,4\n2025-01-03,A,8\n2025-01-03,B,4\n"}, "12"),
    Case("sealed-tally", "math", 5,
         "Let a(0)=2 and a(1)=5. For n from 2 to 10 inclusive, "
         "a(n) is the least nonnegative residue of 3*a(n-1) + a(n-2) + n modulo 19. "
         "Return only an integer giving the sum of a(6) through a(10) inclusive.", {}, "57"),
    Case("alternating-cards", "math", 6,
         "How many permutations of the six distinct cards A, B, C, D, E, F have A before D, "
         "have B and C adjacent in either order, and place F in an even position (positions start at 1)? "
         "Return only an integer.", {}, "60"),
    Case("published-index", "search", 3,
         "Required input file: `current-index.md`.\n"
         "If it is unavailable, use `published.md` instead. Return only the catalogue label.",
         {"published.md": "Catalogue label: Tideglass\n"}, "Tideglass"),
    Case("spectrum-audit", "search", 3,
         "Read the supplied file `spectra.txt`.\n"
         "The audit uses the entry with median strength. Return only its label.",
         {"spectra.txt": "Nacre 29\nSpindle 9\nThistle 17\n"}, "Thistle"),
    Case("vault-close", "data_analysis", 9,
         "Required input file: `vault/closing.csv`.\n"
         "Sum its settlement column for accounts marked closed. Return only an integer.",
         {"vault/opening.csv": "account,settlement,status\nC1,18,closed\nC2,33,open\n"}, None),
    Case("revision-audit", "search", 8,
         "Use the supplied directory `revisions`.\n"
         "Inspect the individual revision notes inside it and report the last approved revision code. "
         "Return only the code.",
         {"revisions": "This inventory marker contains no revision notes.\n"}, None),
)
PAIR = (CASES[0], CASES[2])


def build_source(source, cases):
    source.mkdir()
    for case in cases:
        task = source / case.key
        task.mkdir()
        (task / "task.json").write_text(json.dumps({
            "task_id": case.key, "task_type": case.kind, "reward": case.reward, "instruction": case.instruction,
        }), encoding="utf-8")
        for name, contents in case.files.items():
            path = task / "workspace" / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")


def score_run(output, cases, baseline):
    manifest = load_manifest(output)
    outcomes = []
    for case in cases:
        path = output / "tasks" / case.key / "answer.txt"
        actual = path.read_bytes() if path.is_file() and not path.is_symlink() else None
        passed = case.answer is not None and actual == (case.answer + "\n").encode()
        outcomes.append({"task_id": case.key, "solvable": case.answer is not None,
                         "passed": passed, "score": case.reward if passed else 0})
    totals = {
        "tasks": len(cases), "correct": sum(item["passed"] for item in outcomes),
        "score": sum(item["score"] for item in outcomes), "max_score": sum(case.reward for case in cases),
    }
    write_jsonl(output / "score.jsonl", [*outcomes, totals])
    manifest["scoring"] = {"verifier_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
                           "independent_solvability_labels": True}
    summary = generate(output, output, manifest, baseline)
    return summary, json.loads((output / "metrics.json").read_text())


@contextmanager
def test_double(cases):
    sys.path.insert(0, str(ROOT / "tests"))
    from test_budget import INCLUDED, KEY, MODEL, proxy

    def respond(request):
        query = next(message["content"] for message in request["messages"] if message["role"] == "user")
        case = next(case for case in cases if query.startswith(case.instruction))
        observed = sum(message["role"] == "tool" for message in request["messages"])
        paths = list(case.files) if case.answer is not None else [
            "vault/closing.csv" if case.key == "vault-close" else "revisions",
        ]
        if observed < len(paths):
            return tool_call("read_file", {"path": paths[observed]}, observed)
        if case.answer is None:
            return {"role": "assistant", "content": "The required resource cannot be read."}
        return tool_call("submit_answer", {"answer": case.answer}, observed)

    with proxy(*[(200, INCLUDED, respond, None)] * (4 * len(cases))) as (url, _):
        with patch.dict(os.environ, LLM_BASE_URL=url, LLM_API_KEY=KEY, LLM_MODEL=MODEL):
            yield


def tool_call(name, args, index):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": f"call-{index}", "type": "function",
        "function": {"name": name, "arguments": json.dumps(args)},
    }]}


def display(value):
    return "N/A" if value is None else str(value)


def compare(args):
    args.output.mkdir(parents=True)
    attainable = sum(case.reward for case in CASES if case.answer is not None)
    maximum = sum(case.reward for case in CASES)
    lines = [
        "# Сравнение F и G", "",
        f"Прокси — {args.proxy_kind}. Режим B+C, сессий на прогон — {args.sessions}.",
        "В смешанной выборке 2 из 10 задач имеют проверяемый блокер. "
        f"Потолок достижимых баллов — {attainable} из {maximum}. Разметка применяется после прогона.",
        "Отказом считается CONFIRMED или SUSPECTED. При нуле отказов доля ошибочных отказов не определена.",
        "Расход приведён в выходном эквиваленте. Время распознавания — сумма длительностей локальных проверок.",
        "",
        "| Набор | Режим | Лимит | Баллы | Решено | CONFIRMED | SUSPECTED | UNCERTAIN | NEED_MORE_INFO | BUDGET_EXCEEDED | FAILED | Ошибочные отказы | Доля | До отказов | На невыполнимые | Проверки, мс | Всего | Секунды |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    pair_ok = True
    valid = True
    for label, cases, limit in (("mixed", CASES, args.mixed_budget), ("pair", PAIR, args.pair_budget)):
        source = args.output / f"{label}-source"
        build_source(source, cases)
        for enabled in (False, True):
            mode = "G" if enabled else "F"
            output = args.output / f"{label}-{mode}"
            baseline = args.output / f"{label}-F" if enabled else None
            prepare(SimpleNamespace(run_dir=output, source_dir=source, archive=None))
            context = test_double(cases) if args.proxy_kind == "test-double" else nullcontext()
            with context:
                run(SimpleNamespace(
                    run_dir=output, image=None, network=None, baseline=baseline, local_budget=limit, mode="B+C",
                    planner="F", early_stop=enabled, sessions=args.sessions, run_timeout=args.timeout - 5,
                    proxy_kind=args.proxy_kind, proxy_budget=None, timeout=args.timeout,
                ))
            summary, metrics = score_run(output, cases, baseline)
            recognition = metrics["early_stop"]
            if recognition is None:
                valid = False
                lines.append(f"| {label} | [{mode}]({output.name}/report.md) | {limit / 10} | N/A |")
                continue
            statuses = recognition["statuses"]
            ratio = recognition["false_refusal_fraction"]
            lines.append("| " + " | ".join(map(display, (
                label, f"[{mode}]({output.name}/report.md)", limit / 10, summary["score"], summary["correct"],
                *(statuses.get(status, 0) for status in (
                    "UNSOLVABLE_CONFIRMED", "UNSOLVABLE_SUSPECTED", "UNCERTAIN", "NEED_MORE_INFO",
                    "BUDGET_EXCEEDED", "FAILED",
                )),
                recognition["false_refusals"], f"{ratio:.1%}" if ratio is not None else None,
                recognition["until_refusal"]["measured_tenths"] / 10,
                recognition["known_unsolvable_spend"]["measured_tenths"] / 10,
                recognition["recognition_ms"], summary["measured_output_equivalent"], summary["seconds"],
            ))) + " |")
            if label == "pair":
                pair_ok &= summary["correct"] == 2
            rows, _ = read_jsonl(output / "results-by-task.jsonl")
            valid &= len(rows) == len(cases) and all(row["resolution_final"] for row in rows)
            if enabled and label == "mixed":
                valid &= statuses.get("UNSOLVABLE_CONFIRMED") == sum(case.answer is None for case in cases)
                valid &= recognition["false_refusals"] == 0
                valid &= all(not row["submitted"] for row in rows if row["solvable"] is False)
    lines.extend([
        "",
        f"Оба ответа в парном сценарии подтверждены в обоих режимах — {'да' if pair_ok else 'нет'}.",
        f"Итоговые записи и проверка отказов G — {'пройдены' if valid else 'есть отклонения'}.",
        "",
    ])
    write_text(args.output / "comparison.md", "\n".join(lines))
    print(args.output / "comparison.md")
    return 0 if pair_ok and valid else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=lambda name: Path(name).resolve())
    parser.add_argument("--proxy-kind", required=True, choices=("test-double", "organizer"))
    parser.add_argument("--mixed-budget", required=True, type=budget_tenths)
    parser.add_argument("--pair-budget", required=True, type=budget_tenths)
    parser.add_argument("--sessions", type=int, choices=range(1, 6), default=1)
    parser.add_argument("--timeout", type=int, choices=range(10, 3571), default=180, metavar="SECONDS")
    return compare(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
