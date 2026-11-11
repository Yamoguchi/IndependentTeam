"""Summarize stage 11 matrices and draw observed score against actual spend."""

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import mean, median

from stage11_matrix import ABLATIONS, ARTIFACTS, PERCENTS

from contest_harness.evaluation.artifacts import read_jsonl, write_json, write_text
from contest_harness.evaluation.budget_curve import chart, retrospective
from contest_harness.evaluation.metrics import context_stats, token_rows

FIELDS = {
    "score": "Баллы", "correct": "Решено", "success_rate": "Доля успеха",
    "output_equivalent": "Расход", "input_tokens": "Вход", "output_tokens": "Выход",
    "cached_input_tokens": "Кеш", "reasoning_tokens": "Reasoning",
    "calls": "Вызовы LLM", "seconds": "Время агента, с",
    "output_equivalent_per_point": "Расход на балл",
    "points_per_1000_output_equivalent": "Баллы на 1 000 экв.",
    "tool_errors": "Ошибки инструментов", "escalations": "Эскалации",
}
PREDECESSOR = {
    "A": "baseline", "B": "A", "C": "B", "D": "C", "E1": "C", "E2": "C",
    "E3": "E2", "F": "control", "G": "F", "F-5": "F",
}


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def fmt(value):
    if value is None:
        return "N/A"
    return f"{value:,.3f}".rstrip("0").rstrip(".") if isinstance(value, float) else str(value)


def delta(before, after):
    if before is None or after is None:
        return "N/A (нет измерения)"
    diff = after - before
    if not before:
        return f"{diff:+,.3f} (база 0, процент неприменим)"
    return f"{diff:+,.3f} ({diff / before:+.2%})"


def relative(output, target):
    return target.relative_to(output.parent).as_posix()


def link(output, target, name):
    return f"[{name}]({relative(output, target)})"


def task_diagnostics(run):
    tasks, _ = read_jsonl(run / "results-by-task.jsonl")
    trace, _ = read_jsonl(run / "trace.jsonl")
    calls = token_rows(trace)
    by_index, calls_by_index = defaultdict(list), defaultdict(list)
    for row in trace:
        by_index[row.get("task_index")].append(row)
    for row in calls:
        calls_by_index[row.get("task_index")].append(row)
    contexts = {
        t["task_id"]: context_stats(by_index[t["task_index"]], calls_by_index[t["task_index"]])
        for t in tasks if t["started"]
    }

    def rank(key):
        pairs = [(key(task), task["task_id"]) for task in tasks if task["started"]]
        return ", ".join(f"`{name}` ({fmt(value)})" for value, name in sorted(
            ((value, name) for value, name in pairs if value is not None), reverse=True
        )[:3]) or "N/A (нет начатых задач с измерением)"

    errors = Counter(f"{event['tool']} / {event['status']} / {event.get('error_type', 'код/результат')}"
                     for event in trace if event["event"] == "tool.finished"
                     and event["status"] in {"error", "invalid"})
    unavailable = Counter()
    requests = {(event.get("task_index"), event.get("call_id")):
                {item["name"] for item in event["tools"]["items"]}
                for event in trace if event["event"] == "llm.request"}
    for event in trace:
        if event["event"] == "tool.finished" and event["status"] == "invalid":
            advertised = requests.get((event.get("task_index"), event.get("call_id")))
            if advertised is not None and event["tool"] not in advertised:
                unavailable[event["tool"]] += 1
    return {
        "Расход": rank(lambda task: task["usage"]["output_equivalent"]),
        "Число вызовов": rank(lambda task: task["usage"]["calls"]),
        "Повторный вход, токены": rank(lambda task: contexts[task["task_id"]]["repeat_input_tokens"]["sum"]),
        "Повторные результаты инструментов, байты": rank(
            lambda task: (contexts[task["task_id"]]["retransmitted_bytes"] or {}).get("tool_messages")),
        "Причины ошибок и недопустимых вызовов": str(dict(errors.most_common(5))),
        "Отказы по отсутствующей в запросе схеме": str(dict(unavailable)),
    }


def details(lines, out, run):
    m = load(run / "metrics.json")
    by_type = load(run / "results-by-type.json")
    lines.extend([f"### {link(out, run / 'report.md', run.name)}", "",
                  "| Тип | Баллы | Вызовы | Расход | Средний расход | Среднее / медиана / P90, с |",
                  "| --- | ---: | ---: | ---: | ---: | --- |"])
    for kind, item in by_type.items():
        cost = item["task_output_equivalent"]
        timing = item["time"]["task_seconds"]
        lines.append(f"| {kind} | {item['score']} | {item['calls']} | {fmt(item['output_equivalent'])} | "
                     f"{fmt(cost['mean'])} | " + " / ".join(fmt(timing[k]) for k in ("mean", "median", "p90")) + " |")
    t = m["time"]
    c = m["context"]
    trace, _ = read_jsonl(run / "trace.jsonl")
    responses = [row["response"] for row in trace if row["event"] == "llm.finished" and row.get("response")]
    reasoning_bytes = sum((row.get("reasoning") or {}).get("utf8_bytes", 0) for row in responses)
    argument_bytes = sum((call.get("arguments") or {}).get("utf8_bytes", 0)
                         for row in responses for call in row.get("tool_calls", []))
    p = m["planning"]
    r = m["routes"]
    g = m["early_stop"]
    budget = m["budget"]
    lines.extend([
        "",
        f"- Учёт usage {m['measured_calls']}/{m['calls']}. Вход/выход {m['input_tokens']}/{m['output_tokens']}, "
        f"кеш/reasoning {m['cached_input_tokens']}/{m['reasoning_tokens']}. "
        f"Расход на успешную задачу {fmt(m['output_equivalent_per_successful_task'])}. "
        "Среднее / медиана / P90 на начатую задачу "
        + " / ".join(fmt(m["task_output_equivalent"][key]) for key in ("mean", "median", "p90")) + ".",
        f"- Контекст первых вызовов сумма/среднее {fmt(c['first_input_tokens']['sum'])}/{fmt(c['first_input_tokens']['mean'])}. "
        f"Повторных {fmt(c['repeat_input_tokens']['sum'])}/{fmt(c['repeat_input_tokens']['mean'])}. "
        f"Доля повторного входа {c['repeat_input_fraction']:.1%}, изменение окна между вызовами "
        f"{fmt(c['input_tokens_change']['mean'])} токена в среднем. "
        f"Повторная передача в байтах {c['retransmitted_bytes']}. "
        f"Байты ответа LLM по полезным полям: текст {fmt(c['response_content_bytes']['sum'])}, "
        f"reasoning {reasoning_bytes}, аргументы инструментов {argument_bytes}. "
        f"Вызовов на начатую задачу {fmt(m['calls_per_attempted_task'])}.",
        f"- Суммы времени LLM и инструментов {fmt(t['model_ms']['sum'])} и {fmt(t['tool_ms']['sum'])} мс. "
        f"Медиана / P95 / максимум одного вызова LLM {fmt(t['model_ms']['median'])} / "
        f"{fmt(t['model_ms']['p95'])} / {fmt(t['model_ms']['max'])} мс. "
        f"Время запуска {fmt(m['launcher_wall_seconds'])} с, сессий {m['sessions']}, пик {m['peak_active_sessions']}.",
        f"- Бюджет использован на {budget['local_occupied_fraction']:.1%} по занятости. "
        f"Остаток {budget['remaining_tenths']/10:,.1f}, превышение {budget['local_overrun_tenths']/10:,.1f}.",
        f"- Маршруты {r['by_route']}. Вызовы по моделям {m['models']}. "
        f"Доли локальных и решений начальной моделью {fmt(r['local_solution_fraction'])} и "
        f"{fmt(r['initial_model_solution_fraction'])}. Переходы {r['escalations']}, "
        f"частота {fmt(r['escalation_fraction'])}, успешность "
        + (fmt(r["escalation_success_fraction"]) if r["escalations"] else "N/A (переходов не было)") + ".",
    ])
    if r["escalations"]:
        lines.append(f"- Расход до и после эскалаций {fmt(r['before_escalation']['output_equivalent'])} / "
                     f"{fmt(r['after_escalation']['output_equivalent'])}. Без прироста баллов {r['escalations_without_gain']}.")
    else:
        lines.append("- Расход до и после эскалаций N/A (переходов не было). Переходов без прироста 0.")
    if p:
        full = p["full_positive_costs"]
        lines.append(f"- План F. Пропуски {p['skipped']}, ошибки допуска {p['admitted_with_budget_denial']}, "
                     f"расход выше прогноза у {p['cost_overruns']} задач. MAE/MAPE завершённых "
                     f"{fmt(full['mae'])}/{fmt(full['mape_percent'])}% (n={full['samples']}). "
                     f"Калибровка {'N/A (нет независимых оценок P)' if not p['probability_samples'] else fmt(p['brier_score'])}.")
    else:
        lines.append("- Прогноз и калибровка N/A (режим без планировщика).")
    if g:
        fraction = g["false_refusal_fraction"]
        why = "нет отказов" if not g["refusals"] else "нет меток выполнимости"
        lines.append(f"- Итоги выполнимости {g['statuses']}, причины {g['reasons']}. "
                     f"Время распознавания {g['recognition_ms']} мс, "
                     f"измерено до отказов {g['until_refusal']['measured_tenths']/10:,.1f}. "
                     f"Доля ложных отказов {f'{fraction:.1%}' if fraction is not None else f'N/A ({why})'}.")
    else:
        lines.append("- Итоги раннего отказа N/A (проверка G выключена).")
    lines.extend(["", "| Инструмент | Вызовы | Успех | Ошибки / доля | Недопустимые / доля | Повторы | "
                  "Среднее, мс | Всего байт |",
                  "| --- | ---: | ---: | --- | --- | ---: | ---: | ---: |"])
    for tool, stats in m["tools"].items():
        finished = stats["finished"]
        bad = stats["invalid_actions"]
        bad_rate = f"{bad/finished:.1%}" if bad is not None and finished else "N/A (нет статуса)"
        lines.append(f"| {tool} | {stats['calls']} | {stats['ok']} | {stats['errors']} / "
                     f"{fmt(stats['error_rate'])} | {fmt(bad)} / {bad_rate} | {stats['repeated_arguments']} | "
                     f"{fmt(stats['duration_ms']['mean'])} | {fmt(stats['output_bytes']['sum'])} |")
    lines.append("")
    lines.extend(f"- {label}. {value}." for label, value in task_diagnostics(run).items())
    lines.append("")


def summarize(root, output):
    datasets = (
        ("Смешанный (10)", root / "mixed-ablation", root / "mixed-extra",
         [root / "mixed-curve", root / "mixed-A-curve"]),
        ("Сохранённый (100)", root / "archive-corrected/archive-ablation",
         root / "archive-corrected/archive-extra",
         [root / "archive-corrected/archive-curve", root / "archive-corrected/archive-E1-curve"]),
    )
    lines = ["# Матрица этапа 11", "", "Все замеры в этом файле выполнены на macOS ARM64, "
             "Python 3.12, openJiuwen 0.1.16.post20, через управляемый HTTP-двойник. "
             "Двойник возвращает заранее подготовленные действия. Usage синтетический и зависит от фактически "
             "переданных сообщений, схем и ответа. Задержка HTTP 20 мс. Время параллельных вызовов суммируется "
             "по событиям и перекрывается. Полное время запуска измерено по часам стенда.", ""]
    curves = {}
    for label, ablation, extra, curve_dirs in datasets:
        samples = load(ablation / "matrix.json")
        additions = (extra, ablation.parent / "parallel-extra")
        for directory in additions:
            if (directory / "matrix.json").exists():
                samples += load(directory / "matrix.json")
        points = [row for path in curve_dirs if (path / "matrix.json").exists()
                  for row in load(path / "matrix.json")]
        curves[label] = points
        base = samples[0]
        distinct = [row for row in samples if row["name"] in ABLATIONS]
        def rank(candidate):
            repeats = [row for row in samples if row["mode"] == candidate["mode"]]
            return -median(row["score"] for row in repeats), median(row["exact_tenths"] for row in repeats)

        winner = min(distinct, key=rank)
        tied = [row["name"] for row in distinct if rank(row) == rank(winner)]
        required = {"F", "G", winner["name"]}
        for mode in required:
            limits = {row["limit_tenths"] for row in points if row["mode"] == mode}
            assert limits == {base["exact_tenths"] * p // 100 for p in PERCENTS}, f"Incomplete sweep: {label} / {mode}"
        manifest = load(ablation / "baseline/run-manifest.json")
        model = manifest["runtime"]["model"]
        lines.extend([
            f"## {label}", "",
            f"Лимит {base['limit_tenths']/10:,.1f}, модель `{model}`, "
            f"SHA-256 очищенных входов `{base['input_sha256']}`. "
            "Флаги, версии и SHA-256 кода записаны в манифестах отдельных запусков. "
            "A–D используют стандартную сдачу. E1 и E2 сравниваются с C по одному флагу, "
            "E3 добавляет каскад к E2. F сравнивается с control при E3, G и F-5 меняют один параметр F.",
            "",
            "| Режим | Локальные баллы | Решено | Расход | Δ расхода к baseline | Δ баллов к baseline | "
            "LLM | Вход | Выход | Сокращение входа | Время, с |",
            "| --- | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ])
        for row in distinct:
            name = row["name"]
            lines.append(f"| {link(output, ablation / name / 'report.md', name)} | {row['score']} | {row['correct']} | "
                         f"{fmt(row['output_equivalent'])} | {delta(base['output_equivalent'], row['output_equivalent'])} | "
                         f"{delta(base['score'], row['score'])} | {row['calls']} | {row['input_tokens']} | "
                         f"{row['output_tokens']} | {1-row['input_tokens']/base['input_tokens']:+.2%} | {fmt(row['seconds'])} |")
        lines.extend(["", f"Локальный кандидат при этом лимите — **{winner['name']}** "
                      f"с {winner['score']} баллами за {fmt(winner['output_equivalent'])} выходных эквивалентов."
                      + (f" Полное равенство у {', '.join(tied)}. Выбран режим с меньшим числом включённых механизмов."
                         if len(tied) > 1 else ""),
                      "", "| Сравнение | Δ баллов | Δ расхода | Δ вызовов | Δ времени |",
                      "| --- | --- | --- | --- | --- |"])
        index = {row["name"]: row for row in distinct}
        for name, prev in PREDECESSOR.items():
            if name in index and prev in index:
                lines.append(f"| {name} / {prev} | " + " | ".join(
                    delta(index[prev][key], index[name][key]) for key in ("score", "output_equivalent", "calls", "seconds")
                ) + " |")
        lines.extend(["", "| Повторы | n | Баллы среднее / медиана / min–max | "
                      "Расход среднее / медиана / min–max | Время агента среднее / медиана / min–max, с |",
                      "| --- | ---: | --- | --- | --- |"])
        for mode in dict.fromkeys(("baseline", "F", "F-5", "G", winner["name"])):
            runs = [row for row in samples if row["mode"] == mode]
            cells = []
            for field in ("score", "output_equivalent", "seconds"):
                values = [row[field] for row in runs]
                cells.append(f"{fmt(mean(values))} / {fmt(median(values))} / {fmt(min(values))}–{fmt(max(values))}")
            lines.append(f"| {mode} | {len(runs)} | " + " | ".join(cells) + " |")
        lines.extend(["", "### Сравнение кандидата с baseline", "",
                      "| Метрика | Baseline | Кандидат | Δ |", "| --- | ---: | ---: | --- |"])
        for key, name in FIELDS.items():
            lines.append(f"| {name} | {fmt(base[key])} | {fmt(winner[key])} | {delta(base[key],winner[key])} |")
        lines.extend(["", "### Бюджетные прогоны", "",
                      "| Режим | Лимит | Баллы | Решено | Фактический расход | Факт / лимит | "
                      "Остаток после удержаний | Порядок |", "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |"])
        for directory in curve_dirs:
            if not (directory / "matrix.json").exists():
                continue
            for row in load(directory / "matrix.json"):
                run = directory / row["name"]
                m = load(run / "metrics.json")
                occupied = m["budget"]["remaining_tenths"]
                spend = row["output_equivalent"]
                cap = row["limit_tenths"] / 10
                fraction = f"{spend/cap:.1%}" if spend is not None else "N/A (неполный usage)"
                lines.append(f"| {link(output, run/'report.md', row['name'])} | {fmt(cap)} | {row['score']} | "
                             f"{row['correct']} | {fmt(spend)} | {fraction} | {fmt(occupied / 10 if occupied is not None else None)} | "
                             f"{link(output, run/'metrics.json', str(len(m['execution_order'])) + ' задач')} |")
        caps = [base["exact_tenths"] * percent // 100 for percent in PERCENTS]
        lines.extend(["", "### Ретроспективная верхняя граница", "",
                      "| Источник измеренных цен | n | " + " | ".join(f"{p}%" for p in PERCENTS) + " |",
                      "| --- | ---: | " + " | ".join("---:" for _ in caps) + " |"])
        for mode in dict.fromkeys(("baseline", "F", "G", winner["name"])):
            values, known = retrospective(ablation / mode, caps)
            lines.append(f"| {mode} | {known} | " + " | ".join(map(str, values)) + " |")
        lines.extend(["", "Точное решение задачи о рюкзаке 0/1 по подтверждённым ответам данного набора "
                      "и одного опорного прогона указанного режима. Цена равна полному известному расходу задачи. "
                      "Задачи без подтверждения и без полной цены исключены. Граница относится к выбору среди этих "
                      "ответов при фиксированных ценах. Онлайн-резервы, стоимость неудачных попыток, время, "
                      "другие траектории и скрытые тесты в эту модель не входят.", ""])
        for mode in dict.fromkeys(("baseline", "F", "G", winner["name"])):
            details(lines, output, ablation / mode)
        paths = [next(parent / row["name"] for parent in (ablation, *additions)
                      if (parent / row["name"]).is_dir()) for row in samples]
        paths += [p for directory in curve_dirs if (directory / "matrix.json").exists()
                  for p in (directory / row["name"] for row in load(directory / "matrix.json"))]
        assert all((path / item).is_file() for path in paths for item in ARTIFACTS)
        assert {row["input_sha256"] for row in [*samples, *points]} == {base["input_sha256"]}
        assert all(row["usage_complete"] and row["exact_tenths"] <= row["limit_tenths"] for row in [*samples, *points])
        runtimes = [load(path / "run-manifest.json")["runtime"] for path in paths]
        for key in ("python", "sdk_version", "model", "code_sha256"):
            assert all(runtime[key] == runtimes[0][key] for runtime in runtimes), f"Changed runtime {key}: {label}"
        lines.extend([f"Комплект этапа 01 присутствует у всех {len(paths)} прогонов. SHA-256 входов совпадает.", ""])
    lines.extend(["## Score vs Budget", "", "На оси X показан расход, возвращённый двойником, на оси Y — "
                  "локальные баллы после независимой проверки. Проценты относятся к установленным лимитам, "
                  "указанным в таблицах. Совпадающие точки перекрываются.", "",
                  "![Score vs Budget](score-vs-budget.svg)", ""])
    chart(output.parent / "score-vs-budget.svg", curves)
    write_json(output.parent / "score-vs-budget.json", curves)
    write_text(output, "\n".join(lines))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    summarize(args.root, args.root / "matrix-report.md")
