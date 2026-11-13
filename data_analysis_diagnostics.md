# Диагностика Data Analysis и план v1.3

1. Зафиксировать контрольную точку по сохранённым трассам и повторить modified на тех же 25 задачах Data Analysis.
2. Проверить Local-first при одинаковых настройках решателя с обычными и компактными результатами инструментов.
3. Сохранить экономящие вызовы механизмы из текущего харнесса, добавить независимые переключатели и проверить конкурсную точку входа.
4. Проверить качество отдельно от расхода на публичных примерах и смешанном наборе с отсутствующими входами.

## Исторические прогоны

Использованы журналы `logs/experiments/live-api-100.bK5IrQ/historical` и `optimized`. SHA-256 очищенных входов в обоих манифестах совпадает. В обоих прогонах на Data Analysis набрано 75/75. Baseline шёл в Docker, modified — нативно на macOS. Прогоны различались промптом, порядком задач, настройками инструментов и лимитами генерации, поэтому из этой пары нельзя изолировать эффект одной настройки.

- Расход вырос на **4 422,7** токена в выходном эквиваленте. Дополнительные 27 547 входных токенов дали 2 754,7 (+62,3% от разницы). Выходные дали оставшиеся 1 668. Токенов рассуждения и обрезанных завершений в этих задачах не было.
- 25 первых запросов подорожали на 1 975 входных токенов (197,5 в выходном эквиваленте). В каждой отправке JSON-схемы занимали 1 545 → 1 580 байт, а сериализованное системное сообщение 450 → 725 байт. Остальные 25 572 дополнительных входных токена пришлись на последующие запросы. Байты JSON показывают размер компонента запроса. Прокси не предоставляет разбивку токенов по компонентам.
- Вызовы Python выросли с 25 до 41. В 19 задачах modified зафиксирована ошибка Python. Следующие после этих ошибок 19 запросов стоили суммарно 3 454,6 токена. Это сумма полных запросов, в том числе обычной сдачи, поэтому она не равна чистым потерям от ошибок. Наибольший прирост у `data_analysis_0023`, `0005` и `0016`.
- Объём сообщений инструментов, повторно сериализованных в запросах, вырос с 10 433 до 31 717 байт. Контекстный фильтр ни разу не сократил запрос в этой категории. По хешам аргументов точных повторов нет. Смысловую эквивалентность разных фрагментов Python по трассе установить невозможно.
- Трасса хранит размеры и отпечатки вывода. Локально подтверждены ошибки при попытке импортировать `pandas` и при записи `answer.txt` из Python. Отпечатки этих воспроизведений не совпали с сохранёнными ошибками. Причины конкретных исторических ошибок и гипотеза об избыточных рассуждениях остаются неподтверждёнными.

## Что сохранено из modified

| Механизм | Файлы | Наблюдение и ограничение |
| --- | --- | --- |
| Прямая публикация после исходных тестов и для точных локальных проверок | `agent/react.py`, `agent/submission.py`, `workspace/evidence.py`, `workspace/reference.py` | В исторической паре для Coding вызовы LLM снизились со 127 до 100. Все 25 modified-ответов сохранились без вызова `submit_answer`. Расход категории уменьшился на 2 064,1. Проверка зависит от наличия подходящего исходного теста или независимого вычисления. |
| Узкий набор инструментов и выполнение вычислений в Python | `workspace/tools.py`, `workspace/sandbox.py` | В Math вызовы снизились с 71 до 67, входные токены с 76 500 до 55 607, расход на 2 022,3. Две публикации modified не прошли локальную оценку. Файловые математические задачи получают инструменты чтения. |
| Поиск по файлам и компактные результаты с доступом по идентификатору | `workspace/tools.py`, `workspace/output.py`, `agent/context.py` | В Search вывод инструментов снизился с 28 525 до 17 245 байт, расход на 869,9. Для двух задач modified не хватило резерва следующего запроса. Полные сохранённые результаты доступны в рамках задачи. |
| Контроль генерации и восстановление после обрезания | `agent/generation.py`, `agent/model.py` | Границы профилей можно менять независимо. Для Data Analysis в исторической паре обрезаний не обнаружено. Восстановление при обрезании требует нового оплачиваемого вызова. |

Цифры по категориям относятся ко всей исторической конфигурации. Независимый вклад каждого механизма в них не измерен.

## Живое сравнение v1.3

Все строки ниже относятся к одним 25 очищенным задачам Data Analysis, модели `qwen3.8-27b-noreason` и нативному запуску. Баллы выставлены отдельной проверкой по исходным метаданным. Время включает обращения к прокси. У запуска modified лимит 20 000, у каждого варианта minimal — 13 500. Полные прогоны завершились без отказов по локальному лимиту.

| Режим | Решено | Баллы | Вызовы LLM | Input | Output | Расход | Ошибки инструментов | Время, с |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Modified, свежий контроль | 25/25 | 75/75 | 58 | 60 142 | 3 698 | 9 712,2 | 16 | 165,416 |
| Minimal без Local-first и без сжатия | 25/25 | 75/75 | 59 | 53 407 | 3 540 | 8 880,7 | 14 | 133,201 |
| Minimal с Local-first без сжатия | 25/25 | 75/75 | 51 | 42 346 | 2 836 | **7 070,6** | 9 | 117,474 |
| Minimal с Local-first и сжатием | 25/25 | 75/75 | 57 | 51 870 | 3 210 | 8 397,0 | 12 | 133,916 |

Local-first без сжатия уменьшил расход на 27,2% против свежего modified и на 20,4% против minimal без Local-first. Вариант со сжатием израсходовал на 18,8% больше при тех же 75 баллах. Поэтому в Data Analysis сжатие по умолчанию выключено. Для крупных таблиц доступно явное включение и структурированное представление CSV. Разница между двумя историческими modified и свежим контролем показывает вариативность прогонов. Суммарный эффект оценён по одному завершённому прогону каждого варианта.

В первой серии запуск без Local-first прервался после HTTP 429. В таблице приведён повтор этого варианта отдельным запуском. Контроль modified использует ревизию `a418351`. Журналы этого замера находятся в `/tmp/openjiuwen-v13-control.8kuPIt/run` и `/tmp/openjiuwen-v13-live-2peygaep`.

Для повторения сравнения подготовьте три копии входов с помощью `benchmark.py prepare --source-dir SOURCE --run-dir RUN`. Выполните три запуска по очереди и после каждого запустите `benchmark.py check --source-dir SOURCE --run-dir RUN`.

```bash
python benchmark.py run --run-dir RUN_A --mode minimal --direct-publish --local-budget 13500 \
  --feature local_first_enabled=off --feature tool_output_reduction_enabled=off
python benchmark.py run --run-dir RUN_B --mode minimal --direct-publish --local-budget 13500 \
  --feature tool_output_reduction_enabled=off
python benchmark.py run --run-dir RUN_C --mode minimal --direct-publish --local-budget 13500 \
  --feature tool_output_reduction_enabled=on
```

## Проверка конкурсного профиля

| Набор | Проверка | Результат |
| --- | --- | --- |
| 8 публичных примеров | Живой прокси, нативный запуск | 8/8, 20/20 баллов, 3 573,3 токена |
| 8 публичных примеров | Итоговый Docker-образ, точка входа жюри и двойник прокси в изолированной сети | 8/8, 20/20 баллов, исходные тесты совпали по байтам и прошли |
| 2 публичных примера | Живой прокси, общий лимит 1 000 | 2/2, 8/8 баллов, 504,0 токена |
| 2 публичных примера | Docker и двойник прокси, общий лимит 1 000 | 2/2, 407,0 синтетического выходного эквивалента |
| 2 публичных примера | Docker и HTTP 503 после первого решения | Первый `answer.txt` сохранился, второй не создан |
| 10 смешанных задач | Живой прокси, отдельные эталоны и разметка выполнимости | 8/8 выполнимых, 39/56 баллов, 2 803,1 токена за 80,962 с. Два отсутствующих обязательных входа обнаружены без вызовов модели. |

На смешанном наборе первая версия получила 32 балла за 2 409,3 токена. Модель ошиблась в поиске кратчайшего пути. Указание выполнять численные задачи поиска в Python подняло результат до 39 баллов. Первый вариант этого указания расходовал 6 307,8 токена, из которых 3 988,5 пришлись на одну задачу. В её трассе четыре успешных вызова Python завершились с пустым выводом. В итоговом варианте промпт просит печатать результат, а успешное выполнение без вывода даёт короткую подсказку. Итоговый прогон получил те же 39 баллов за 2 803,1 токена.

В Docker-проверке на лимите 650 консервативный резерв не допустил последний запрос второй задачи при остатке 362,8. В прогонах на лимите 1 000 обе задачи опубликованы. Холодная сборка зависимостей и образа заняла 82,53 с. Итоговый образ Linux ARM64 занимает 1,24 ГБ. В образе проверено отсутствие локального `.env`, файлов ключей и метаданных задач.

## Воспроизведение отчёта

```bash
python scripts/compare_data_analysis.py \
  logs/experiments/live-api-100.bK5IrQ/historical \
  logs/experiments/live-api-100.bK5IrQ/optimized
```

Скрипт сопоставляет прогоны по отпечатку очищенных входов и требует полного учёта расхода. Итерации видны в последовательности действий. Расход каждого запроса берётся из `llm.finished`.

## Сводка по трассам

| Показатель | Baseline | Modified |
| --- | ---: | ---: |
| input | 50521 | 78068 |
| output | 3039 | 4707 |
| reasoning | 0 | 0 |
| first input | 19779 | 21754 |
| calls | 55 | 70 |
| generation length | 0 | 0 |
| tool schema bytes | 84975 | 110600 |
| system message bytes | 24750 | 50750 |
| tool message bytes | 10433 | 31717 |
| tool calls | 55 | 70 |
| Python calls | 25 | 41 |
| tool errors | 0 | 19 |
| repeated calls | 0 | 0 |
| tool result bytes | 5156 | 14504 |
| task seconds | 258.478 | 256.492 |

## Расход по задачам

| task_id | baseline_cost | modified_cost | difference | baseline_calls | modified_calls | baseline_input | modified_input | baseline_output | modified_output |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| data_analysis_0023 | 286.9 | 1201.3 | +914.4 | 2 | 6 | 1699 | 7893 | 117 | 412 |
| data_analysis_0005 | 285.4 | 847.2 | +561.8 | 2 | 4 | 1694 | 4902 | 116 | 357 |
| data_analysis_0016 | 286.9 | 799.4 | +512.5 | 2 | 4 | 1699 | 4884 | 117 | 311 |
| data_analysis_0012 | 484.0 | 822.5 | +338.5 | 3 | 4 | 3270 | 5195 | 157 | 303 |
| data_analysis_0014 | 283.2 | 574.8 | +291.6 | 2 | 3 | 1692 | 3378 | 114 | 237 |
| data_analysis_0001 | 275.5 | 566.5 | +291.0 | 2 | 3 | 1685 | 3375 | 107 | 229 |
| data_analysis_0004 | 284.5 | 565.5 | +281.0 | 2 | 3 | 1695 | 3335 | 115 | 232 |
| data_analysis_0010 | 284.3 | 563.8 | +279.5 | 2 | 3 | 1693 | 3338 | 115 | 230 |
| data_analysis_0003 | 284.7 | 558.8 | +274.1 | 2 | 3 | 1697 | 3328 | 115 | 226 |
| data_analysis_0008 | 283.0 | 530.4 | +247.4 | 2 | 3 | 1690 | 3284 | 114 | 202 |
| data_analysis_0006 | 464.6 | 578.5 | +113.9 | 3 | 3 | 3266 | 3635 | 138 | 215 |
| data_analysis_0019 | 275.5 | 327.9 | +52.4 | 2 | 2 | 1685 | 2009 | 107 | 127 |
| data_analysis_0017 | 284.3 | 330.1 | +45.8 | 2 | 2 | 1693 | 2011 | 115 | 129 |
| data_analysis_0013 | 284.5 | 328.1 | +43.6 | 2 | 2 | 1695 | 2011 | 115 | 127 |
| data_analysis_0022 | 283.2 | 326.8 | +43.6 | 2 | 2 | 1692 | 2008 | 114 | 126 |
| data_analysis_0018 | 461.4 | 499.2 | +37.8 | 3 | 3 | 3244 | 3492 | 137 | 150 |
| data_analysis_0009 | 285.6 | 321.5 | +35.9 | 2 | 2 | 1696 | 2005 | 116 | 121 |
| data_analysis_0024 | 465.3 | 497.5 | +32.2 | 3 | 3 | 3263 | 3585 | 139 | 139 |
| data_analysis_0011 | 304.9 | 329.2 | +24.3 | 2 | 2 | 1769 | 2012 | 128 | 128 |
| data_analysis_0007 | 276.4 | 291.1 | +14.7 | 2 | 2 | 1684 | 1841 | 108 | 107 |
| data_analysis_0000 | 463.8 | 476.6 | +12.8 | 3 | 3 | 3268 | 3166 | 137 | 160 |
| data_analysis_0002 | 302.3 | 299.9 | -2.4 | 2 | 2 | 1763 | 1849 | 126 | 115 |
| data_analysis_0020 | 303.4 | 299.9 | -3.5 | 2 | 2 | 1764 | 1849 | 127 | 115 |
| data_analysis_0015 | 294.8 | 285.8 | -9.0 | 2 | 2 | 1758 | 1838 | 119 | 102 |
| data_analysis_0021 | 302.7 | 291.5 | -11.2 | 2 | 2 | 1767 | 1845 | 126 | 107 |

## Инструменты по задачам

| task_id | Время, с | Вызовы инструментов | Ошибки | Повторы аргументов | Вывод, байт |
| --- | ---: | ---: | ---: | ---: | ---: |
| data_analysis_0023 | 3.982 → 16.506 | 2 → 6 | 0 → 1 | 0 → 0 | 29 → 915 |
| data_analysis_0005 | 6.446 → 11.925 | 2 → 4 | 0 → 1 | 0 → 0 | 29 → 766 |
| data_analysis_0016 | 7.238 → 21.768 | 2 → 4 | 0 → 1 | 0 → 0 | 29 → 843 |
| data_analysis_0012 | 10.297 → 12.566 | 3 → 4 | 0 → 1 | 0 → 0 | 731 → 1464 |
| data_analysis_0014 | 9.813 → 10.002 | 2 → 3 | 0 → 1 | 0 → 0 | 27 → 713 |
| data_analysis_0001 | 12.793 → 6.526 | 2 → 3 | 0 → 1 | 0 → 0 | 28 → 717 |
| data_analysis_0004 | 9.362 → 14.676 | 2 → 3 | 0 → 1 | 0 → 0 | 28 → 613 |
| data_analysis_0010 | 27.448 → 9.69 | 2 → 3 | 0 → 1 | 0 → 0 | 29 → 619 |
| data_analysis_0003 | 5.093 → 11.426 | 2 → 3 | 0 → 1 | 0 → 0 | 28 → 613 |
| data_analysis_0008 | 9.891 → 7.161 | 2 → 3 | 0 → 1 | 0 → 0 | 28 → 599 |
| data_analysis_0006 | 21.466 → 6.691 | 3 → 3 | 0 → 1 | 0 → 0 | 739 → 1067 |
| data_analysis_0019 | 4.495 → 4.618 | 2 → 2 | 0 → 1 | 0 → 0 | 28 → 481 |
| data_analysis_0017 | 4.746 → 18.032 | 2 → 2 | 0 → 1 | 0 → 0 | 29 → 482 |
| data_analysis_0013 | 4.271 → 4.808 | 2 → 2 | 0 → 1 | 0 → 0 | 27 → 480 |
| data_analysis_0022 | 4.522 → 12.093 | 2 → 2 | 0 → 1 | 0 → 0 | 28 → 481 |
| data_analysis_0018 | 17.61 → 18.229 | 3 → 3 | 0 → 1 | 0 → 0 | 727 → 995 |
| data_analysis_0009 | 14.416 → 6.643 | 2 → 2 | 0 → 1 | 0 → 0 | 29 → 482 |
| data_analysis_0024 | 7.681 → 9.999 | 3 → 3 | 0 → 1 | 0 → 0 | 733 → 1001 |
| data_analysis_0011 | 8.246 → 5.901 | 2 → 2 | 0 → 1 | 0 → 0 | 214 → 482 |
| data_analysis_0007 | 5.737 → 6.504 | 2 → 2 | 0 → 0 | 0 → 0 | 29 → 29 |
| data_analysis_0000 | 15.659 → 9.052 | 3 → 3 | 0 → 0 | 0 → 0 | 733 → 548 |
| data_analysis_0002 | 4.171 → 6.393 | 2 → 2 | 0 → 0 | 0 → 0 | 214 → 29 |
| data_analysis_0020 | 7.554 → 5.37 | 2 → 2 | 0 → 0 | 0 → 0 | 214 → 29 |
| data_analysis_0015 | 6.021 → 3.506 | 2 → 2 | 0 → 0 | 0 → 0 | 213 → 28 |
| data_analysis_0021 | 29.52 → 16.407 | 2 → 2 | 0 → 0 | 0 → 0 | 213 → 28 |

## Последовательность действий

<details><summary>data_analysis_0023</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 178 | LLM | 793 | 88 | 167.3 | ok/tool_calls |
| 2 | 178 | run_python | — | — | — | ok, 16 B |
| 3 | 179 | LLM | 906 | 29 | 119.6 | ok/tool_calls |
| 4 | 179 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 111 | LLM | 872 | 102 | 189.2 | ok/tool_calls |
| 2 | 111 | run_python | — | — | — | error, 469 B |
| 3 | 112 | LLM | 1145 | 105 | 219.5 | ok/tool_calls |
| 4 | 112 | run_python | — | — | — | ok, 210 B |
| 5 | 113 | LLM | 1348 | 25 | 159.8 | ok/tool_calls |
| 6 | 113 | list_files | — | — | — | ok, 9 B |
| 7 | 114 | LLM | 1392 | 63 | 202.2 | ok/tool_calls |
| 8 | 114 | run_python | — | — | — | ok, 64 B |
| 9 | 115 | LLM | 1492 | 88 | 237.2 | ok/tool_calls |
| 10 | 115 | run_python | — | — | — | ok, 150 B |
| 11 | 116 | LLM | 1644 | 29 | 193.4 | ok/tool_calls |
| 12 | 116 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0005</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 139 | LLM | 791 | 87 | 166.1 | ok/tool_calls |
| 2 | 139 | run_python | — | — | — | ok, 16 B |
| 3 | 140 | LLM | 903 | 29 | 119.3 | ok/tool_calls |
| 4 | 140 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 58 | LLM | 870 | 109 | 196.0 | ok/tool_calls |
| 2 | 58 | run_python | — | — | — | error, 470 B |
| 3 | 59 | LLM | 1151 | 75 | 190.1 | ok/tool_calls |
| 4 | 59 | run_python | — | — | — | ok, 232 B |
| 5 | 60 | LLM | 1347 | 144 | 278.7 | ok/tool_calls |
| 6 | 60 | run_python | — | — | — | ok, 51 B |
| 7 | 61 | LLM | 1534 | 29 | 182.4 | ok/tool_calls |
| 8 | 61 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0016</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 163 | LLM | 793 | 88 | 167.3 | ok/tool_calls |
| 2 | 163 | run_python | — | — | — | ok, 16 B |
| 3 | 164 | LLM | 906 | 29 | 119.6 | ok/tool_calls |
| 4 | 164 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 86 | LLM | 872 | 100 | 187.2 | ok/tool_calls |
| 2 | 86 | run_python | — | — | — | error, 469 B |
| 3 | 87 | LLM | 1143 | 69 | 183.3 | ok/tool_calls |
| 4 | 87 | run_python | — | — | — | ok, 233 B |
| 5 | 88 | LLM | 1333 | 113 | 246.3 | ok/tool_calls |
| 6 | 88 | run_python | — | — | — | ok, 128 B |
| 7 | 89 | LLM | 1536 | 29 | 182.6 | ok/tool_calls |
| 8 | 89 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0012</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 154 | LLM | 790 | 26 | 105.0 | ok/tool_calls |
| 2 | 154 | read_file | — | — | — | ok, 519 B |
| 3 | 155 | LLM | 1146 | 104 | 218.6 | ok/tool_calls |
| 4 | 155 | run_python | — | — | — | ok, 199 B |
| 5 | 156 | LLM | 1334 | 27 | 160.4 | ok/tool_calls |
| 6 | 156 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 77 | LLM | 869 | 97 | 183.9 | ok/tool_calls |
| 2 | 77 | run_python | — | — | — | error, 467 B |
| 3 | 78 | LLM | 1135 | 87 | 200.5 | ok/tool_calls |
| 4 | 78 | run_python | — | — | — | ok, 531 B |
| 5 | 79 | LLM | 1447 | 92 | 236.7 | ok/tool_calls |
| 6 | 79 | run_python | — | — | — | ok, 453 B |
| 7 | 80 | LLM | 1744 | 27 | 201.4 | ok/tool_calls |
| 8 | 80 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0014</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 159 | LLM | 791 | 87 | 166.1 | ok/tool_calls |
| 2 | 159 | run_python | — | — | — | ok, 14 B |
| 3 | 160 | LLM | 901 | 27 | 117.1 | ok/tool_calls |
| 4 | 160 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 81 | LLM | 870 | 99 | 186.0 | ok/tool_calls |
| 2 | 81 | run_python | — | — | — | error, 467 B |
| 3 | 82 | LLM | 1138 | 111 | 224.8 | ok/tool_calls |
| 4 | 82 | run_python | — | — | — | ok, 233 B |
| 5 | 83 | LLM | 1370 | 27 | 164.0 | ok/tool_calls |
| 6 | 83 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0001</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 131 | LLM | 791 | 79 | 158.1 | ok/tool_calls |
| 2 | 131 | run_python | — | — | — | ok, 15 B |
| 3 | 132 | LLM | 894 | 28 | 117.4 | ok/tool_calls |
| 4 | 132 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 50 | LLM | 870 | 100 | 187.0 | ok/tool_calls |
| 2 | 50 | run_python | — | — | — | error, 468 B |
| 3 | 51 | LLM | 1140 | 101 | 215.0 | ok/tool_calls |
| 4 | 51 | run_python | — | — | — | ok, 236 B |
| 5 | 52 | LLM | 1365 | 28 | 164.5 | ok/tool_calls |
| 6 | 52 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0004</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 137 | LLM | 792 | 87 | 166.2 | ok/tool_calls |
| 2 | 137 | run_python | — | — | — | ok, 15 B |
| 3 | 138 | LLM | 903 | 28 | 118.3 | ok/tool_calls |
| 4 | 138 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 55 | LLM | 871 | 96 | 183.1 | ok/tool_calls |
| 2 | 55 | run_python | — | — | — | error, 468 B |
| 3 | 56 | LLM | 1137 | 108 | 221.7 | ok/tool_calls |
| 4 | 56 | run_python | — | — | — | ok, 132 B |
| 5 | 57 | LLM | 1327 | 28 | 160.7 | ok/tool_calls |
| 6 | 57 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0010</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 150 | LLM | 791 | 86 | 165.1 | ok/tool_calls |
| 2 | 150 | run_python | — | — | — | ok, 16 B |
| 3 | 151 | LLM | 902 | 29 | 119.2 | ok/tool_calls |
| 4 | 151 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 72 | LLM | 870 | 98 | 185.0 | ok/tool_calls |
| 2 | 72 | run_python | — | — | — | error, 469 B |
| 3 | 73 | LLM | 1139 | 103 | 216.9 | ok/tool_calls |
| 4 | 73 | run_python | — | — | — | ok, 137 B |
| 5 | 74 | LLM | 1329 | 29 | 161.9 | ok/tool_calls |
| 6 | 74 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0003</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 135 | LLM | 793 | 87 | 166.3 | ok/tool_calls |
| 2 | 135 | run_python | — | — | — | ok, 15 B |
| 3 | 136 | LLM | 904 | 28 | 118.4 | ok/tool_calls |
| 4 | 136 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 106 | LLM | 872 | 92 | 179.2 | ok/tool_calls |
| 2 | 106 | run_python | — | — | — | error, 468 B |
| 3 | 107 | LLM | 1134 | 106 | 219.4 | ok/tool_calls |
| 4 | 107 | run_python | — | — | — | ok, 132 B |
| 5 | 108 | LLM | 1322 | 28 | 160.2 | ok/tool_calls |
| 6 | 108 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0008</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 146 | LLM | 790 | 86 | 165.0 | ok/tool_calls |
| 2 | 146 | run_python | — | — | — | ok, 15 B |
| 3 | 147 | LLM | 900 | 28 | 118.0 | ok/tool_calls |
| 4 | 147 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 67 | LLM | 869 | 98 | 184.9 | ok/tool_calls |
| 2 | 67 | run_python | — | — | — | error, 468 B |
| 3 | 68 | LLM | 1137 | 76 | 189.7 | ok/tool_calls |
| 4 | 68 | run_python | — | — | — | ok, 118 B |
| 5 | 69 | LLM | 1278 | 28 | 155.8 | ok/tool_calls |
| 6 | 69 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0006</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 141 | LLM | 791 | 26 | 105.1 | ok/tool_calls |
| 2 | 141 | read_file | — | — | — | ok, 527 B |
| 3 | 142 | LLM | 1153 | 85 | 200.3 | ok/tool_calls |
| 4 | 142 | run_python | — | — | — | ok, 199 B |
| 5 | 143 | LLM | 1322 | 27 | 159.2 | ok/tool_calls |
| 6 | 143 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 62 | LLM | 870 | 111 | 198.0 | ok/tool_calls |
| 2 | 62 | run_python | — | — | — | error, 779 B |
| 3 | 63 | LLM | 1274 | 77 | 204.4 | ok/tool_calls |
| 4 | 63 | run_python | — | — | — | ok, 275 B |
| 5 | 64 | LLM | 1491 | 27 | 176.1 | ok/tool_calls |
| 6 | 64 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0019</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 170 | LLM | 791 | 79 | 158.1 | ok/tool_calls |
| 2 | 170 | run_python | — | — | — | ok, 15 B |
| 3 | 171 | LLM | 894 | 28 | 117.4 | ok/tool_calls |
| 4 | 171 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 95 | LLM | 870 | 99 | 186.0 | ok/tool_calls |
| 2 | 95 | run_python | — | — | — | error, 468 B |
| 3 | 96 | LLM | 1139 | 28 | 141.9 | ok/tool_calls |
| 4 | 96 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0017</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 165 | LLM | 791 | 86 | 165.1 | ok/tool_calls |
| 2 | 165 | run_python | — | — | — | ok, 16 B |
| 3 | 166 | LLM | 902 | 29 | 119.2 | ok/tool_calls |
| 4 | 166 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 90 | LLM | 870 | 100 | 187.0 | ok/tool_calls |
| 2 | 90 | run_python | — | — | — | error, 469 B |
| 3 | 91 | LLM | 1141 | 29 | 143.1 | ok/tool_calls |
| 4 | 91 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0013</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 157 | LLM | 792 | 88 | 167.2 | ok/tool_calls |
| 2 | 157 | run_python | — | — | — | ok, 14 B |
| 3 | 158 | LLM | 903 | 27 | 117.3 | ok/tool_calls |
| 4 | 158 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 109 | LLM | 871 | 100 | 187.1 | ok/tool_calls |
| 2 | 109 | run_python | — | — | — | error, 467 B |
| 3 | 110 | LLM | 1140 | 27 | 141.0 | ok/tool_calls |
| 4 | 110 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0022</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 176 | LLM | 791 | 86 | 165.1 | ok/tool_calls |
| 2 | 176 | run_python | — | — | — | ok, 15 B |
| 3 | 177 | LLM | 901 | 28 | 118.1 | ok/tool_calls |
| 4 | 177 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 101 | LLM | 870 | 98 | 185.0 | ok/tool_calls |
| 2 | 101 | run_python | — | — | — | error, 468 B |
| 3 | 102 | LLM | 1138 | 28 | 141.8 | ok/tool_calls |
| 4 | 102 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0018</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 167 | LLM | 790 | 26 | 105.0 | ok/tool_calls |
| 2 | 167 | read_file | — | — | — | ok, 515 B |
| 3 | 168 | LLM | 1143 | 84 | 198.3 | ok/tool_calls |
| 4 | 168 | run_python | — | — | — | ok, 199 B |
| 5 | 169 | LLM | 1311 | 27 | 158.1 | ok/tool_calls |
| 6 | 169 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 92 | LLM | 869 | 97 | 183.9 | ok/tool_calls |
| 2 | 92 | run_python | — | — | — | error, 467 B |
| 3 | 93 | LLM | 1135 | 26 | 139.5 | ok/tool_calls |
| 4 | 93 | read_file | — | — | — | ok, 515 B |
| 5 | 94 | LLM | 1488 | 27 | 175.8 | ok/tool_calls |
| 6 | 94 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0009</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 148 | LLM | 792 | 87 | 166.2 | ok/tool_calls |
| 2 | 148 | run_python | — | — | — | ok, 16 B |
| 3 | 149 | LLM | 904 | 29 | 119.4 | ok/tool_calls |
| 4 | 149 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 70 | LLM | 871 | 92 | 179.1 | ok/tool_calls |
| 2 | 70 | run_python | — | — | — | error, 469 B |
| 3 | 71 | LLM | 1134 | 29 | 142.4 | ok/tool_calls |
| 4 | 71 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0024</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 180 | LLM | 791 | 26 | 105.1 | ok/tool_calls |
| 2 | 180 | read_file | — | — | — | ok, 520 B |
| 3 | 181 | LLM | 1151 | 85 | 200.1 | ok/tool_calls |
| 4 | 181 | run_python | — | — | — | ok, 200 B |
| 5 | 182 | LLM | 1321 | 28 | 160.1 | ok/tool_calls |
| 6 | 182 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 103 | LLM | 870 | 26 | 113.0 | ok/tool_calls |
| 2 | 103 | read_file | — | — | — | ok, 520 B |
| 3 | 104 | LLM | 1230 | 85 | 208.0 | ok/tool_calls |
| 4 | 104 | run_python | — | — | — | error, 468 B |
| 5 | 105 | LLM | 1485 | 28 | 176.5 | ok/tool_calls |
| 6 | 105 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0011</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 152 | LLM | 792 | 99 | 178.2 | ok/tool_calls |
| 2 | 152 | run_python | — | — | — | ok, 201 B |
| 3 | 153 | LLM | 977 | 29 | 126.7 | ok/tool_calls |
| 4 | 153 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 75 | LLM | 871 | 99 | 186.1 | ok/tool_calls |
| 2 | 75 | run_python | — | — | — | error, 469 B |
| 3 | 76 | LLM | 1141 | 29 | 143.1 | ok/tool_calls |
| 4 | 76 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0007</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 144 | LLM | 790 | 79 | 158.0 | ok/tool_calls |
| 2 | 144 | run_python | — | — | — | ok, 16 B |
| 3 | 145 | LLM | 894 | 29 | 118.4 | ok/tool_calls |
| 4 | 145 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 65 | LLM | 869 | 78 | 164.9 | ok/tool_calls |
| 2 | 65 | run_python | — | — | — | ok, 16 B |
| 3 | 66 | LLM | 972 | 29 | 126.2 | ok/tool_calls |
| 4 | 66 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0000</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 128 | LLM | 790 | 26 | 105.0 | ok/tool_calls |
| 2 | 128 | read_file | — | — | — | ok, 521 B |
| 3 | 129 | LLM | 1155 | 84 | 199.5 | ok/tool_calls |
| 4 | 129 | run_python | — | — | — | ok, 199 B |
| 5 | 130 | LLM | 1323 | 27 | 159.3 | ok/tool_calls |
| 6 | 130 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 47 | LLM | 869 | 74 | 160.9 | ok/tool_calls |
| 2 | 47 | run_python | — | — | — | ok, 14 B |
| 3 | 48 | LLM | 966 | 26 | 122.6 | ok/tool_calls |
| 4 | 48 | read_file | — | — | — | ok, 521 B |
| 5 | 49 | LLM | 1331 | 60 | 193.1 | ok/tool_calls |
| 6 | 49 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0002</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 133 | LLM | 790 | 97 | 176.0 | ok/tool_calls |
| 2 | 133 | run_python | — | — | — | ok, 201 B |
| 3 | 134 | LLM | 973 | 29 | 126.3 | ok/tool_calls |
| 4 | 134 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 53 | LLM | 869 | 86 | 172.9 | ok/tool_calls |
| 2 | 53 | run_python | — | — | — | ok, 16 B |
| 3 | 54 | LLM | 980 | 29 | 127.0 | ok/tool_calls |
| 4 | 54 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0020</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 172 | LLM | 790 | 98 | 177.0 | ok/tool_calls |
| 2 | 172 | run_python | — | — | — | ok, 201 B |
| 3 | 173 | LLM | 974 | 29 | 126.4 | ok/tool_calls |
| 4 | 173 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 97 | LLM | 869 | 86 | 172.9 | ok/tool_calls |
| 2 | 97 | run_python | — | — | — | ok, 16 B |
| 3 | 98 | LLM | 980 | 29 | 127.0 | ok/tool_calls |
| 4 | 98 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0015</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 161 | LLM | 791 | 91 | 170.1 | ok/tool_calls |
| 2 | 161 | run_python | — | — | — | ok, 200 B |
| 3 | 162 | LLM | 967 | 28 | 124.7 | ok/tool_calls |
| 4 | 162 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 84 | LLM | 870 | 74 | 161.0 | ok/tool_calls |
| 2 | 84 | run_python | — | — | — | ok, 15 B |
| 3 | 85 | LLM | 968 | 28 | 124.8 | ok/tool_calls |
| 4 | 85 | submit_answer | — | — | — | ok, 13 B |

</details>

<details><summary>data_analysis_0021</summary>

**Baseline**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 174 | LLM | 792 | 98 | 177.2 | ok/tool_calls |
| 2 | 174 | run_python | — | — | — | ok, 200 B |
| 3 | 175 | LLM | 975 | 28 | 125.5 | ok/tool_calls |
| 4 | 175 | submit_answer | — | — | — | ok, 13 B |

**Modified**

| № | Запрос | Действие | Input | Output | Cost | Статус |
| ---: | ---: | --- | ---: | ---: | ---: | --- |
| 1 | 99 | LLM | 871 | 79 | 166.1 | ok/tool_calls |
| 2 | 99 | run_python | — | — | — | ok, 15 B |
| 3 | 100 | LLM | 974 | 28 | 125.4 | ok/tool_calls |
| 4 | 100 | submit_answer | — | — | — | ok, 13 B |

</details>
