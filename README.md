# openJiuwen · базовый харнесс

Харнесс проходит по каталогам задач последовательно, используя одну модель через OpenAI-совместимый API прокси организаторов. На каждом шаге передаётся полная история текущей задачи. Модель может читать файлы, исправлять исходники задач `coding` и запускать Python для вычислений и тестов. Планировщик, отбор задач, сокращение контекста и оценщик выполнимости относятся к следующим этапам.

## Настройка и сборка

Скопируйте `.env.example` в `.env` и укажите `LLM_BASE_URL`, `LLM_API_KEY` и `LLM_MODEL`. `LLM_BASE_URL` — базовый URL API, например `https://proxy.example/v1`. Клиент добавляет путь `chat/completions`. Переменные процесса имеют приоритет перед `.env`. По умолчанию используется `qwen3.8-27b-noreason`. Допустимые значения перечислены в [AGENTS.md](AGENTS.md).

```bash
docker build -t openjiuwen-baseline:local .
```

При запуске жюри точка входа — `python /app/run_agent.py --tasks-dir /data/tasks`. Каждый ответ атомарно сохраняется в `answer.txt` рядом с `task.json`. Для `coding` изменённые исходники остаются в `workspace/`. Python для задач запускается в Linux с Landlock ABI 3 или новее и seccomp. Процесс получает ограниченный доступ к рабочим файлам и запускается с отдельным окружением.

## Воспроизведение замера

Укажите путь к архиву примеров и создайте свежую копию задач. Для `prepare` достаточно установленного Python 3.12. Копия содержит условия и входные файлы. Оригинал с эталонами используется отдельно для проверки.

```bash
ARCHIVE='/absolute/path/to/task-dir.zip'
mkdir -p "$PWD/.runtime"
RUN_DIR=$(mktemp -d "$PWD/.runtime/baseline.XXXXXX")
python3 benchmark.py prepare --archive "$ARCHIVE" --tasks-dir "$RUN_DIR/tasks"

docker run --rm --read-only --tmpfs /tmp:rw,size=2g -e HOME=/tmp \
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 512 \
  --cpus 4 --memory 16g \
  -v "$PWD/.env:/app/.env:ro" -v "$RUN_DIR/tasks:/data/tasks:rw" \
  openjiuwen-baseline:local --tasks-dir /data/tasks > "$RUN_DIR/tokens.jsonl"

docker run --rm --network none --read-only --tmpfs /tmp:rw,size=2g -e HOME=/tmp \
  --cap-drop ALL --security-opt no-new-privileges --entrypoint python \
  -v "$ARCHIVE:/data/examples.zip:ro" -v "$RUN_DIR/tasks:/data/tasks:ro" \
  openjiuwen-baseline:local /app/benchmark.py check \
  --archive /data/examples.zip --tasks-dir /data/tasks > "$RUN_DIR/score.jsonl"
```

`tokens.jsonl` содержит показатели по каждой задаче и итоговую строку `summary`. `output_equivalent` равен `input_tokens / 10 + output_tokens`. Кешированные входные токены входят в `input_tokens`. `reasoning_tokens` выводится отдельно для анализа и учитывается в выходных один раз. Если прокси сообщает отдельные reasoning-токены, их включение определяется по `total_tokens`. `usage_complete: false` означает, что для части вызовов прокси не вернул статистику. Статус `submitted` означает, что файл записан. Корректность ответа и полученные баллы отражены в `score.jsonl`.

На macOS тот же замер можно выполнить из окружения Python 3.12 с установленными `requirements.txt`. Подготовьте свежий `RUN_DIR` по примеру выше. Python для задач запускается через системную песочницу `sandbox-exec` на отдельной копии рабочих файлов.

```bash
.venv/bin/python run_agent.py --tasks-dir "$RUN_DIR/tasks" > "$RUN_DIR/tokens.jsonl"
.venv/bin/python benchmark.py check --archive "$ARCHIVE" --tasks-dir "$RUN_DIR/tasks" > "$RUN_DIR/score.jsonl"
```

## Базовый замер

На macOS модель `qwen3.8-27b-noreason` решила все 8 примеров и набрала 20 из 20 баллов. За 26 запросов прокси учёл 28 644 входных и 1 276 выходных токенов. Расход составил 4 140,4 токена в выходном эквиваленте за 99,3 секунды. Все ответы повторно прошли проверку в Linux-образе в режиме `--read-only`.
