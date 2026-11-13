# Харнесс openJiuwen

`run_agent.py` запускает конкурсный харнесс на базе openJiuwen ReAct. При запуске командой жюри он использует профиль `minimal`, одну сессию и модель `qwen3.8-27b-noreason`. План строится по всем задачам до первого запроса к модели. [Требования соревнования](AGENTS.md)

## Сборка

```bash
BUILD_TREE=$(mktemp -d)
git archive HEAD | tar -xf - -C "$BUILD_TREE"
du -sk "$BUILD_TREE"
/usr/bin/time -p docker build --no-cache -t openjiuwen-harness:local "$BUILD_TREE"
docker images openjiuwen-harness:local --format '{{.Repository}}:{{.Tag}} -> {{.Size}}'
docker run --rm --network none --entrypoint python openjiuwen-harness:local -m pip check
```

Базовый образ Python 3.12.12 закреплён по тегу и digest. openJiuwen 0.1.16.post20 и остальные зависимости устанавливаются при сборке. Для запуска инструментов Python требуется Linux с Landlock ABI 3 или новее и seccomp. [Dockerfile](Dockerfile) · [Версии зависимостей](requirements-linux.txt)

## Запуск жюри

Задайте `LLM_BASE_URL` с базовым URL прокси организаторов и `LLM_API_KEY` в окружении. `TASKS_DIR` должен быть абсолютным путём к доступной на запись копии задач. `PROXY_NETWORK` должна давать контейнеру сетевой доступ только к прокси организаторов. Например, подключите контейнер к внутренней Docker-сети с TCP-реле до прокси и сохраните имя HTTPS-хоста для проверки сертификата.

```bash
docker run --rm --pull never --network "$PROXY_NETWORK" \
  --read-only --tmpfs /tmp:rw,size=2g -e HOME=/tmp \
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 512 --cpus 4 --memory 16g \
  -e LLM_BASE_URL -e LLM_API_KEY \
  -v "$TASKS_DIR:/data/tasks:rw" openjiuwen-harness:local --tasks-dir /data/tasks
```

`ENTRYPOINT` выполняет `python /app/run_agent.py --tasks-dir /data/tasks` из `/app`. Харнесс читает `task.json` в подкаталогах и работает в `workspace/` текущей задачи или в её каталоге, если `workspace/` отсутствует. Дополнительный файл результатов включается через `--results-file` или `OUT`.

Ответ публикуется атомарно в `/data/tasks/<каталог-задачи>/answer.txt` сразу после проверки. Файл содержит итог в формате из условия. Для задач с правками кода исходники сохраняются в смонтированном рабочем каталоге до публикации ответа. Контроллер сверяет исходные тесты и проверяет их запуск после последней правки. Для повторных прогонов используйте свежие копии входов.

## Выбор задач и учёт расхода

- Очередь по умолчанию сортируется по отношению `reward` к ожидаемому расходу. Прогноз использует размер первого запроса и ожидаемое число шагов по типу задачи. Явное отсутствие обязательного входа помечается как `NEED_MORE_INFO`. Неоднозначные случаи передаются в ReAct.
- Единый счётчик учитывает все вызовы. Расход равен входным токенам, включая кешированные, делённым на 10, плюс выходные токены. Токены рассуждений включаются в выход один раз. Транспорт резервирует бюджет до отправки каждого запроса, в том числе при параллельных сессиях, и сверяет резерв со статистикой прокси.
- Общий локальный лимит можно задать через `HARNESS_TOKEN_BUDGET` или `--local-budget`. При отсутствии полной статистики удерживается резерв неизвестной части запроса. Дедлайн по умолчанию составляет 3570 секунд. Опциональный `--sessions` поддерживает до пяти сессий.
- По умолчанию трасса конкурсного запуска создаётся в `/tmp/openjiuwen-run-*`. Через `--trace-file` можно задать новый файл вне каталога задач. Трасса содержит оценки, решения планировщика, резервы, счётчики прокси и события публикации. Команда `python run_agent.py --help` выводит параметры запуска.

## Воспроизводимая локальная проверка

Укажите абсолютный путь `ARCHIVE` к оригинальному `task-dir.zip`. Подготовка создаёт копию задач без `expected`, `check` и прежних ответов. Исходный архив используется для независимой проверки. Для нативной проверки нужен Python 3.12 и зависимости из `requirements.txt`. На Linux можно использовать те же зафиксированные версии, что и в образе, добавив `-c requirements-linux.txt` к команде установки.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
RUN_DIR=$(.venv/bin/python benchmark.py prepare --archive "$ARCHIVE")

.venv/bin/python benchmark.py run --run-dir "$RUN_DIR" \
  --image openjiuwen-harness:local --network "$PROXY_NETWORK" \
  --mode minimal --planner static --direct-publish --timeout 3600

.venv/bin/python benchmark.py check --archive "$ARCHIVE" --run-dir "$RUN_DIR"
```

Если песочница Python недоступна на хосте, выполните проверку внутри образа без сети.

```bash
docker run --rm --network none --read-only --tmpfs /tmp:rw,size=2g -e HOME=/tmp \
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 512 --cpus 4 --memory 16g \
  -v "$PWD/benchmark.py:/app/benchmark.py:ro" \
  -v "$PWD/contest_harness/evaluation:/app/contest_harness/evaluation:ro" \
  -v "$ARCHIVE:/data/examples.zip:ro" -v "$RUN_DIR:/data/run:rw" \
  --entrypoint python openjiuwen-harness:local /app/benchmark.py check \
  --archive /data/examples.zip --run-dir /data/run
```

`prepare` и `check` также принимают `--source-dir` вместо `--archive`. Отчёт в каталоге прогона содержит баллы, время и расход. Оценщик возвращает ненулевой код, если хотя бы одна задача не прошла проверку.

## Тренировочные траектории

Соберите отдельный набор с выполнимыми задачами и примерами с отсутствующими обязательными входами. Прогоните его через `benchmark.py run` и проверьте через `benchmark.py check`. Трассы сохраняются в `journal/trace.jsonl`. Для сравнения используйте свежие копии одного набора и одинаковый `--local-budget`. Сопоставляйте баллы, расход и время по отчётам.

Для исторического планировщика `F` экспортируйте траектории командой `python benchmark.py planning-history --output /tmp/planning-history.jsonl "$RUN_DIR"` и передайте файл через `--planner F --planning-history /tmp/planning-history.jsonl`. Экспорт требует полной трассы, измеренного расхода и независимой проверки результатов. Прогноз для оцениваемых задач исключает наблюдения с совпадающим идентификатором или отпечатком инструкции. Храните траектории вне входных данных и репозитория.
