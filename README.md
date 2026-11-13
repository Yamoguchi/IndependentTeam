# Харнесс openJiuwen

## Сборка и запуск

Соберите образ из корня репозитория. Зависимости, включая openJiuwen 0.1.16.post20, устанавливаются при сборке.

```bash
docker build -t hack:latest .
```

Используйте команду `docker run` из регламента. Она переопределяет точку входа образа через `--entrypoint python`, поэтому после `hack:latest` передаётся `/app/run_agent.py --tasks-dir /data/tasks`. При запуске со штатным `ENTRYPOINT` образа достаточно передать `--tasks-dir /data/tasks`.

`TASKS` должен указывать на каталог, в котором лежат папки задач с `task.json`. Ответы сохраняются в `answer.txt` рядом с каждым `task.json`. Команда жюри работает без `OUT` внутри контейнера. Для выполнения Python-кода в задачах ядро Linux должно поддерживать seccomp и Landlock ABI 3 или новее.

## Оценка расхода и выполнимости

До первого запроса к модели харнесс оценивает расход по размеру первого запроса и ожидаемому числу шагов для типа задачи. Очередь строится по отношению `reward` к прогнозу расхода. Если в условии явно указан обязательный файл или каталог и его нет, задача помечается как `NEED_MORE_INFO` без вызова модели. Неоднозначные случаи передаются агенту.

Перед каждым запросом харнесс резервирует токены и затем сверяет резерв со статистикой прокси. Расход считается как входные токены, включая кешированные, делённые на 10, плюс выходные токены с учётом рассуждений. Для локальной проверки общий лимит можно задать через `HARNESS_TOKEN_BUDGET` или `--local-budget`.

## Траектории и локальная проверка
Трассы содержат прогнозы, шаги агента и расход. Они хранятся локально и используются для сравнения конфигураций.

Для проверки укажите абсолютный путь `ARCHIVE` к оригинальному `task-dir.zip`, экспортируйте `LLM_BASE_URL` и `LLM_API_KEY` и задайте `PROXY_NETWORK` с доступом к прокси. На хосте нужен Python 3.12. Подготовка убирает эталонные ответы и поля проверки из входов агента. Оценщик читает оригинал архива отдельно и запускает исходные тесты в контейнере без сети.

```bash
RUN_DIR=$(python3.12 benchmark.py prepare --archive "$ARCHIVE")
python3.12 benchmark.py run --run-dir "$RUN_DIR" \
  --image hack:latest --network "$PROXY_NETWORK" \
  --mode minimal --planner static --direct-publish

docker run --rm --network none --read-only --tmpfs /tmp:rw,size=2g -e HOME=/tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  -v "$PWD/benchmark.py:/app/benchmark.py:ro" \
  -v "$PWD/contest_harness/evaluation:/app/contest_harness/evaluation:ro" \
  -v "$ARCHIVE:/data/examples.zip:ro" -v "$RUN_DIR:/data/run:rw" \
  --entrypoint python hack:latest /app/benchmark.py check \
  --archive /data/examples.zip --run-dir /data/run
```

Трасса лежит в `$RUN_DIR/journal/trace.jsonl`, результаты проверки в `$RUN_DIR/score.jsonl`. После проверки отдельного тренировочного набора экспортируйте историю в новый файл и передайте его харнессу через `--planner F --planning-history`.

```bash
python3.12 benchmark.py planning-history --output "$RUN_DIR/planning-history.jsonl" "$RUN_DIR"
```
