"""Render the human-readable benchmark report."""

from contest_harness.evaluation.metrics import equivalent

NA = {
    "official_score": "Скрытая проверка жюри не входит в локальный стенд.",
    "local_score": "Требуется независимый score.jsonl.",
    "exact_cost": "Нужна статистика usage по всем вызовам и полный журнал вызовов.",
    "durations": "Длительности есть только для записанных событий завершения.",
    "budget": "Лимиты задаются отдельно. Для исторических прогонов резерв недоступен.",
    "tools": "Исторические статусы инструментов могут отражать наблюдения SDK.",
    "validation": "Раздельные вердикты доступны с этапа 06 в событиях answer.validated.",
    "routes": "Для маршрутов нужна трасса этапа 07, для долей решений и выигрыша нужен независимый score.jsonl.",
    "planning": "Для прогноза нужен журнал этапа 08. При недостатке независимых исходов вероятность отсутствует.",
    "early_stop": "Для исходов нужен журнал F или G, для ошибочных отказов нужна независимая разметка выполнимости.",
    "comparison": "Нужен отчёт baseline с данными по той же метрике. Процент от нуля не определён.",
    "missing_events": "Поля зависят от событий и метаданных, сохранённых в источнике. Нулевой знаменатель также даёт N/A.",
}


def display(value):
    if value is None:
        return "N/A"
    if isinstance(value, float):
        return f"{value:,.6f}".rstrip("0").rstrip(".")
    return str(value)


def top_values(pairs):
    ranked = sorted(((value, name) for name, value in pairs if value is not None), reverse=True)
    return ", ".join(f"`{name}` ({display(value)})" for value, name in ranked[:3]) or "N/A"


def findings(tasks, metrics, grouped, comparison_data):
    started = [row for row in tasks if row["started"]]
    tools = metrics["tools"] or {}
    routes = (metrics["routes"] or {}).get("by_route", {})
    lines = [
        f"- Самые дорогие задачи {top_values((r['task_id'], r['usage']['output_equivalent']) for r in started)}.",
        f"- Самые дорогие категории {top_values((k, v['output_equivalent']) for k, v in grouped.items())}.",
        f"- Больше всего вызовов {top_values((r['task_id'], r['usage']['calls']) for r in started)}.",
        "- Больше всего повторного контекста, байты "
        + top_values((r["task_id"], (r["retransmitted_context_bytes"] or {}).get("messages")) for r in started)
        + ".",
        "- Частые ошибки инструментов "
        + top_values((name, count) for name, row in tools.items()
                     if (count := row["errors"] + (row["invalid_actions"] or 0)))
        + ".",
        "- Эффективные маршруты, баллы на 1 000 токенов "
        + top_values((name, 1000 * r["score"] / r["output_equivalent"]) for name, r in routes.items()
                     if r["score"] is not None and r["output_equivalent"])
        + ".",
    ]
    deltas = comparison_data["deltas"]
    score = deltas.get("score", {}).get("absolute")
    cost = deltas.get("output_equivalent", {}).get("absolute")
    if (comparison_data["same_prepared_content"] is not True or comparison_data["same_conditions"] is not True
            or score is None or cost is None):
        verdict = "N/A. Нужны одинаковые входы и условия запуска, сопоставимые баллы и полный учёт расхода."
    elif score == cost == 0:
        verdict = "Измеримого эффекта по баллам и расходу нет."
    elif score >= 0 and cost <= 0:
        verdict = f"Улучшение. Изменение баллов {score:+g}, изменение расхода {cost:+g}."
    elif score <= 0 and cost >= 0:
        verdict = f"Ухудшение. Изменение баллов {score:+g}, изменение расхода {cost:+g}."
    else:
        verdict = f"Изменение баллов {score:+g} при изменении расхода {cost:+g}."
    lines.append(f"- Эффект относительно baseline. {verdict}")
    if (metrics.get("planning") or {}).get("admitted_with_budget_denial"):
        lines.append("- Сверить прогноз и резерв первого запроса на задачах с отказом в бюджете после допуска.")
    if any(row["errors"] or row["invalid_actions"] for row in tools.values()):
        lines.append("- Разобрать первые по частоте ошибки инструментов по идентификаторам вызовов в трассе.")
    if comparison_data["same_conditions"] is True and score is not None and cost is not None:
        if score < 0 or cost > 0:
            lines.append("- Повторить сравнение на том же лимите и проверить задачи с потерей баллов или ростом расхода.")
    return lines


def markdown(summary, metrics, grouped, comparison_data, manifest, tasks=()):
    launch = manifest.get("launch", {})
    runtime = manifest.get("runtime") or {}
    context = metrics["context"] or {}
    repeated = context.get("retransmitted_bytes") or {}
    selection = context.get("window_selection") or {}
    timing = metrics["time"]
    journal = manifest["journal"]
    lines = [
        "# Отчёт прогона",
        "",
        f"- Статус `{summary['status']}`",
        f"- Входы SHA-256 `{display(manifest['input']['tree_sha256'])}`",
        f"- Ревизия `{display(manifest.get('code', {}).get('git_head'))}`, "
        f"dirty `{display(manifest.get('code', {}).get('git_dirty'))}`",
        f"- Модель `{display((manifest.get('runtime') or {}).get('model') or manifest.get('launch', {}).get('model'))}`",
        f"- Режим `{display(runtime.get('mode') or launch.get('agent_mode'))}`, прокси `{display(launch.get('proxy_kind'))}`",
        f"- Флаг прямой публикации `{display(runtime.get('direct_publish', launch.get('direct_publish')))}`",
        f"- Ранний отказ G `{display(runtime.get('early_stop', launch.get('early_stop')))}`",
        *(
            ["- Счётчики токенов заданы HTTP-двойником. Байты вычислены по фактическим запросам."]
            if launch.get("proxy_kind") == "test-double" else []
        ),
        f"- Среда `{display(launch.get('backend'))}`, Python `{display(runtime.get('python'))}`, openJiuwen `{display(runtime.get('sdk_version'))}`",
        f"- Образ `{display(launch.get('image_id'))}`",
        f"- Лимит прокси {display(metrics['budget']['limit'])} в выходном эквиваленте",
        f"- Локальный лимит {display(equivalent(metrics['budget']['local_limit_tenths']))} в выходном эквиваленте",
        f"- Событий в трассе {journal['trace_events']}, событие завершения `{journal['finished_event']}`, отброшена оборванная строка `{journal['trace_tail_discarded']}`",
        "",
        "| Показатель | Значение |",
        "| --- | ---: |",
    ]
    for label, key in (
        ("Задачи прошли локальную проверку", "correct"),
        ("Всего задач", "tasks"),
        ("Опубликовано ответов", "submitted"),
        ("Локальные баллы", "score"),
        ("Максимум локальных баллов", "max_score"),
        ("Доля успеха", "success_rate"),
        ("Вызовы", "calls"),
        ("Вызовы с usage", "measured_calls"),
        ("Вызовы с неизвестным расходом", "unknown_usage_calls"),
        ("Статус точного расхода", "usage_status"),
        ("Покрытие usage", "usage_coverage"),
        ("Входные токены", "input_tokens"),
        ("Кешированный вход", "cached_input_tokens"),
        ("Выходные токены", "output_tokens"),
        ("Reasoning-токены", "reasoning_tokens"),
        ("Расход в выходном эквиваленте", "output_equivalent"),
        ("Измеренная часть расхода", "measured_output_equivalent"),
        ("Расход на балл", "output_equivalent_per_point"),
        ("Баллы на 1 000 выходных эквивалентов", "points_per_1000_output_equivalent"),
        ("Время агента, с", "seconds"),
        ("Время запуска, с", "launcher_wall_seconds"),
        ("Ошибки инструментов по данным SDK", "tool_errors"),
        ("Вызовы инструментов", "tool_calls"),
        ("Вывод инструментов, байты UTF-8", "tool_output_bytes"),
        ("Компактные результаты инструментов", "compacted_tool_outputs"),
        ("Скрытые повторные результаты инструментов", "repeated_tool_outputs"),
        ("Запросы дополнительных деталей", "detail_requests"),
        ("Ответы модели с finish_reason=length", "generation_truncations"),
        ("Запросы восстановления", "generation_recoveries"),
        ("Отказы в резерве на восстановление", "generation_recovery_denials"),
    ):
        lines.append(f"| {label} | {display(summary[key])} |")
    lines.extend(
        [
            "",
            "## По типам задач",
            "",
            "| Тип | Решено / всего | Баллы / максимум | Вызовы | Расход | Сумма длительностей задач, с |",
            "| --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for name, values in grouped.items():
        lines.append(
            f"| {name} | {display(values['correct'])} / {values['tasks']} | "
            f"{display(values['score'])} / {display(values['max_score'])} | "
            f"{display(values['calls'])} | {display(values['output_equivalent'])} | "
            f"{display(values['time']['task_seconds']['sum'])} |"
        )
    lines.extend(["", "## Контекст, время и бюджет", ""])
    for label, value in (
        ("Среднее число вызовов на начатую задачу", metrics["calls_per_attempted_task"]),
        ("Входные токены первых вызовов", context.get("first_input_tokens", {}).get("sum")),
        ("Средний вход первого вызова, токены", context.get("first_input_tokens", {}).get("mean")),
        ("Входные токены повторных вызовов", context.get("repeat_input_tokens", {}).get("sum")),
        ("Средний вход повторного вызова, токены", context.get("repeat_input_tokens", {}).get("mean")),
        ("Доля входных токенов повторных вызовов", context.get("repeat_input_fraction")),
        ("Среднее изменение входа между вызовами задачи, токены", context.get("input_tokens_change", {}).get("mean")),
        ("Максимальный размер сообщений запроса, байты", context.get("message_bytes", {}).get("max")),
        ("Повторно переданные сообщения, байты", repeated.get("messages")),
        ("Из них результаты инструментов, байты", repeated.get("tool_messages")),
        ("Повторно переданные схемы, байты", repeated.get("tool_schemas")),
        ("Вызовы со сжатием окна", selection.get("compacted")),
        ("Исключённые пары и группы вызовов", selection.get("dropped_exchanges")),
        ("Расход на успешную задачу", metrics["output_equivalent_per_successful_task"]),
        ("Медианный расход на задачу", metrics["task_output_equivalent"]["median"]),
        ("P90 расхода на задачу", metrics["task_output_equivalent"]["p90"]),
        ("Средний расход на начатую задачу", metrics["task_output_equivalent"]["mean"]),
        ("Настроено сессий", metrics["sessions"]),
        ("Пик активных сессий", metrics["peak_active_sessions"]),
        ("Сумма длительностей модели, мс", timing["model_ms"]["sum"]),
        ("Сумма ожиданий бюджета, мс", timing["budget_wait_ms"]["sum"]),
        ("P95 длительности вызова модели, мс", timing["model_ms"]["p95"]),
        ("Сумма длительностей инструментов, мс", timing["tool_ms"]["sum"]),
        ("Медиана / максимум длительности вызова, мс",
         f"{display(timing['model_ms']['median'])} / {display(timing['model_ms']['max'])}"),
        ("Среднее / медиана / P90 времени задачи, с",
         " / ".join(display(timing["task_seconds"][key]) for key in ("mean", "median", "p90"))),
        ("Доля модели в сумме измеренных длительностей", timing["model_fraction"]),
        ("Доля инструментов в сумме измеренных длительностей", timing["tool_fraction"]),
        ("Доля использованного бюджета", metrics["budget"]["used_fraction"]),
        ("Остаток внешнего бюджета", metrics["budget"]["remaining"]),
        ("Удержанный резерв в выходном эквиваленте", metrics["budget"]["reservation"]),
        ("Из него восстановлено по незавершённым вызовам", equivalent(metrics["budget"]["recovered_in_flight_tenths"])),
        ("Занято по локальному допуску", equivalent(metrics["budget"]["occupied_tenths"])),
        ("Доля занятого локального лимита", metrics["budget"]["local_occupied_fraction"]),
        ("Остаток локального лимита", equivalent(metrics["budget"]["remaining_tenths"])),
        ("Превышение локального лимита", equivalent(metrics["budget"]["local_overrun_tenths"])),
        ("Вызовы с превышением оценки", summary["estimate_overruns"]),
        ("Сумма превышений оценок в выходном эквиваленте", equivalent(summary["estimate_overrun_tenths"])),
        ("Отклонено по локальному лимиту", summary["denied_calls"]),
        ("Другие отказы до отправки", summary["not_sent_calls"]),
    ):
        lines.append(f"- {label} {display(value)}")
    lines.extend(["", "## Инструменты", ""])
    if metrics["tools"]:
        lines.extend(
            [
                "| Инструмент | Вызовы | Успехи | Ошибки (доля) | Недопустимые (доля) | Повторы | Среднее, мс | Вывод, байты | Причины недоступности |",
                "| --- | ---: | ---: | --- | --- | ---: | ---: | ---: | --- |",
            ]
        )
        for name, row in metrics["tools"].items():
            lines.append(
                f"| {name} | {row['calls']} | {row['ok']} | {row['errors']} ({display(row['error_rate'])}) | "
                f"{display(row['invalid_actions'])} ({display(row['invalid_action_rate'])}) | "
                f"{row['repeated_arguments']} | {display(row['duration_ms']['mean'])} | "
                f"{display(row['output_bytes']['sum'])} | {display(row['unavailable_reasons'])} |"
            )
    else:
        lines.append("N/A. В журнале нет событий вызова инструментов.")
    lines.extend(["", "## Валидация ответа", ""])
    if validation := metrics.get("answer_validation"):
        lines.extend([
            f"- Проверено кандидатов {validation['attempts']}, отклонено {validation['rejected']}.",
            f"- Формат {display(validation['format'])}.",
            f"- Семантика {display(validation['semantic'])}.",
            f"- Каналы публикации {display(validation['publication_sources'])}.",
        ])
    else:
        lines.append("N/A. В журнале нет раздельных вердиктов.")
    lines.extend(["", "## Маршрутизация", ""])
    if routes := metrics["routes"]:
        lines.extend(["| Маршрут | Задачи | Подтверждено | Баллы |", "| --- | ---: | ---: | ---: |"])
        for name, row in routes["by_route"].items():
            lines.append(f"| {name} | {row['tasks']} | {display(row['correct'])} | {display(row['score'])} |")
        lines.extend([
            "",
            f"- Начальные модели по задачам {display(routes['initial_models'])}.",
            f"- Модели после перехода по задачам {display(routes['escalated_models'])}.",
            f"- Вызовы по моделям {display(metrics['models'])}.",
            f"- Локальные публикации {routes['local_publications']}.",
            f"- Доля локальных среди подтверждённых решений {display(routes['local_solution_fraction'])}.",
            f"- Доля локальных среди входных задач {display(routes['local_fraction_of_tasks'])}.",
            f"- Доля начальной модели среди подтверждённых решений {display(routes['initial_model_solution_fraction'])}.",
            f"- Переходы {routes['escalations']}, доля среди маршрутизированных задач {display(routes['escalation_fraction'])}.",
            f"- Доля подтверждённых эскалаций {display(routes['escalation_success_fraction'])}.",
            f"- Переходы без прироста баллов {display(routes['escalations_without_gain'])}.",
            f"- Переходы без независимой оценки {routes['escalations_unchecked']}.",
            f"- Баллы на эскалированных задачах {display(routes['escalation_points'])}.",
            f"- Расход эскалированных задач до перехода {display(routes['before_escalation']['output_equivalent'])}.",
            f"- Расход эскалированных задач после перехода {display(routes['after_escalation']['output_equivalent'])}.",
            f"- Решения об эскалации {display(routes['decisions'])}.",
        ])
    else:
        lines.append("N/A. В журнале нет событий маршрутизации.")
    lines.extend(["", "## Планирование", ""])
    if planning := metrics["planning"]:
        observed = planning["known_costs"]
        full = planning["full_costs"]
        lines.extend([
            f"- Режим {planning['mode']}, предварительная оценка {planning['preflight_ms']} мс.",
            f"- Исходный порядок {' → '.join(planning['input_order'])}.",
            f"- Плановый порядок {' → '.join(planning['planned_order'])}.",
            f"- Фактический порядок {' → '.join(metrics['execution_order']) or 'N/A'}.",
            f"- Пропуски {display(planning['skipped'])}.",
            f"- Ошибки планирования и отказ в резерве после допуска {planning['planning_errors']}.",
            f"- Допущено с последующим отказом в резерве {planning['admitted_with_budget_denial']}.",
            f"- Изменился начальный маршрут или модель {planning['planned_route_changed']}.",
            f"- Факт выше прогноза у {planning['cost_overruns']} задач на "
            f"{planning['cost_overrun_total']} в выходном эквиваленте суммарно.",
            f"- MAE {display(observed['mae'])} и MAPE, % {display(observed['mape_percent'])} "
            f"по {observed['samples']} / {observed['mape_samples']} наблюдениям, включая оборванные попытки.",
            f"- MAE {display(full['mae'])} и MAPE, % {display(full['mape_percent'])} "
            f"по {full['samples']} / {full['mape_samples']} завершённым попыткам.",
            f"- Brier Score {display(planning['brier_score'])}, Log Loss {display(planning['log_loss'])}, "
            f"исходов с прогнозом {planning['probability_samples']}.",
            f"- Калибровка {display(planning['calibration'])}.",
            "",
            "| Задача | Исходный / плановый / факт | Награда | Приоритет | P успеха | Прогноз | Допуск | Факт | Локально прогноз / факт, мс | Остаток до / после | Итог | Баллы |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: |",
        ])
        for row in tasks:
            plan = row["plan"] or {}
            admission = row["planning_decision"] or {}
            before = equivalent(admission.get("remaining_tenths"))
            after = equivalent((row["budget_after"] or {}).get("remaining_tenths"))
            lines.append(
                f"| {row['task_id']} | {row['input_index']} / {display(plan.get('planned_index'))} / "
                f"{display(row['task_index'])} | {row['reward']} | {display(row['priority'])} | "
                f"{display(row['success_probability'])} | {display(row['estimated_cost'])} | "
                f"{display(equivalent(admission.get('admission_tenths')))} | {display(row['actual_cost'])} | "
                f"{display(row['estimated_local_ms'])} / {display(row['local_ms'])} | "
                f"{display(before)} / {display(after)} | {row['stop_reason'] or row['status']} | "
                f"{display(row['score'])} |"
            )
        lines.extend(["", "Оценки расхода, допуска, факта и остатка приведены в выходном эквиваленте."])
    else:
        lines.append("N/A. В журнале нет предварительного плана.")
    lines.extend(["", "## Распознавание невыполнимости", ""])
    if recognition := metrics["early_stop"]:
        statuses = recognition["statuses"]
        lines.extend([
            f"- Итоги {display(statuses)}.",
            f"- Причины {display(recognition['reasons'])}.",
            f"- Отказы {recognition['refusals']}, ошибочные {display(recognition['false_refusals'])}, "
            f"доля среди подтверждённых и предполагаемых отказов {display(recognition['false_refusal_fraction'])}.",
            f"- Проверки обязательных предпосылок {recognition['preflight_checks']} "
            f"за {recognition['recognition_ms']} мс суммарно.",
            f"- Расход до отказов {display(equivalent(recognition['until_refusal']['exact_tenths']))}, "
            f"измеренная часть {display(equivalent(recognition['until_refusal']['measured_tenths']))}, "
            f"удержано {display(equivalent(recognition['until_refusal']['held_tenths']))}.",
            f"- Измеренный расход по задачам без опубликованного ответа "
            f"{display(equivalent(recognition['until_non_completion']['measured_tenths']))}.",
            f"- Повторные попытки {recognition['retries']}, решения о повторе {display(recognition['retry_decisions'])}.",
            "",
            "| Задача | Итог | Причина | Измерено до остановки |",
            "| --- | --- | --- | ---: |",
        ])
        for row in tasks:
            if row["resolution"]:
                resolution = row["resolution"]
                lines.append(
                    f"| {row['task_id']} | {resolution['status']} | {resolution['reason']} | "
                    f"{display(equivalent(row['usage']['measured_tenths']))} |"
                )
    else:
        lines.append("N/A. В журнале нет итогов распознавания.")
    lines.extend(["", "## Сравнение", ""])
    if comparison_data["baseline"]:
        lines.extend(
            [
                f"Совпадение очищенного содержимого входов: {display(comparison_data['same_prepared_content'])}.",
                f"Совпадение модели, типа прокси, среды запуска и лимита: {display(comparison_data['same_conditions'])}.",
                "",
                "| Метрика | Baseline | Прогон | Δ | Δ, % |",
                "| --- | ---: | ---: | ---: | ---: |",
            ]
        )
        for key, delta in comparison_data["deltas"].items():
            lines.append(
                "| "
                + " | ".join(
                    [
                        key,
                        *(display(delta[field]) for field in ("baseline", "current", "absolute", "percent")),
                    ]
                )
                + " |"
            )
        lines.extend(
            [
                "",
                f"Сокращение входного контекста (доля) {display(comparison_data['context_input_reduction_fraction'])}.",
            ]
        )
    else:
        lines.append("N/A. Отдельный отчёт для сравнения не задан.")
    if previous := comparison_data.get("previous_stage"):
        lines.extend(["", "### Сравнение с предыдущим этапом", "",
                      "| Метрика | До | После | Δ | Δ, % |", "| --- | ---: | ---: | ---: | ---: |"])
        for key, delta in previous["deltas"].items():
            lines.append("| " + " | ".join(
                [key, *(display(delta[field]) for field in ("baseline", "current", "absolute", "percent"))]
            ) + " |")
    lines.extend(["", "## Результаты при ограниченных бюджетах", ""])
    if series := manifest.get("budget_series"):
        lines.append("[График Score vs Budget](../score-vs-budget.svg)")
        lines.extend(["", "| Лимит | Баллы | Решено | Расход | Доля лимита | Остаток | Ретроспективная граница |",
                      "| ---: | ---: | ---: | ---: | ---: | ---: | ---: |"])
        for point in series:
            lines.append("| " + " | ".join(display(point[key]) for key in (
                "limit", "score", "correct", "cost", "fraction", "remaining", "oracle",
            )) + " |")
        lines.extend(["", "Ретроспективная граница использует известные цены подтверждённых решений.",
                      "Порядок выполнения доступен в metrics.json каждой точки серии."])
    else:
        lines.append("N/A. Для шести лимитов требуется отдельная серия `stage11_matrix.py curve`.")
    lines.extend(["", "## Выводы", "", *findings(tasks, metrics, grouped, comparison_data)])
    lines.extend(["", "## Интерпретация N/A", ""])
    lines.extend(f"- {reason}" for reason in NA.values())
    lines.append("")
    return "\n".join(lines)
