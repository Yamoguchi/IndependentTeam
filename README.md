# Харнесс openJiuwen

## Сборка

Откройте терминал в корне репозитория и соберите Docker-образ.

```bash
docker build -t openjiuwen-harness:local .
```

## Запуск по команде жюри

Перед запуском передайте адрес и ключ прокси организаторов через `LLM_BASE_URL` и `LLM_API_KEY`. В `TASKS_DIR` укажите полный путь к каталогу, куда вы распаковали задачи. Контейнер будет записывать ответы в этот каталог. В `PROXY_NETWORK` укажите имя Docker-сети, из которой доступен только прокси организаторов.

```bash
docker run --rm --pull never --network "$PROXY_NETWORK" \
  --read-only --tmpfs /tmp:rw,size=2g -e HOME=/tmp \
  --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 512 --cpus 8 --memory 16g \
  -e LLM_BASE_URL -e LLM_API_KEY \
  -v "$TASKS_DIR:/data/tasks:rw" \
  openjiuwen-harness:local --tasks-dir /data/tasks
```

Docker запустит `python /app/run_agent.py --tasks-dir /data/tasks` из каталога `/app`. Готовый ответ появится в файле `answer.txt` рядом с `task.json` соответствующей задачи.
