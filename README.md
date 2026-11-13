# Харнесс openJiuwen

- Соберите образ из корня репозитория командой `docker build -t openjiuwen-harness:local .`.
- В команде `docker run` из регламента укажите тег собранного образа. После тега укажите только `--tasks-dir /data/tasks`. Вызов Python уже задан в `ENTRYPOINT`.
- Для выполнения Python-кода внутри задач нужны seccomp и Landlock ABI 3 или новее.
