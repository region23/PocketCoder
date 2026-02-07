# PocketCoder

`PocketCoder` состоит из двух процессов:

- `pocketcoder-daemon` (FastAPI + runner + sqlite)
- `pocketcoder-bot` (Telegram control-plane клиент)

## Реализовано сейчас

- Daemon API:
  - `GET /health`
  - `GET /ready`
  - `GET /metrics`
  - `GET /engines`
  - `GET /cli/tools`
  - `POST /cli/tools/{name}/install`
  - `POST /cli/tools/{name}/update`
  - `POST /cli/tools/install-missing`
  - `POST /cli/tools/update-all`
  - `POST /jobs`
  - `GET /jobs`
  - `GET /jobs/{id}`
  - `POST /jobs/{id}/cancel`
  - `POST /jobs/{id}/input`
  - `GET /ready` возвращает `503`, если не готова БД или не хватает свободного диска
- Job lifecycle: `STARTING -> RUNNING -> WAITING_INPUT -> COMPLETED/FAILED/CANCELLED/TIMEOUT`.
- Ограничение: только 1 активная job на репозиторий.
- Восстановление после рестарта: активные job помечаются как `LOST`.
- SQLite:
  - `jobs`
  - `engines`
  - `repos`
  - `artifacts`
- Runner:
  - запуск subprocess без shell
  - отдельная process group
  - PTY-режим для engines с `requires_pty=true`
  - timeout и cancel (SIGTERM/SIGKILL)
  - запись stdout/stderr в отдельные файлы
  - size-based rotation логов (`POCKETCODER_LOG_MAX_BYTES`, `POCKETCODER_LOG_BACKUP_COUNT`)
  - gzip логов/сегментов по завершению + регистрация логов как artifacts
- Git (если workspace уже git-репозиторий):
  - checkout новой ветки `pc/<repo>/<timestamp>` для job
  - best-effort commit итоговых изменений
  - сохранение `commit_hash` в job
  - optional push при `POCKETCODER_GIT_PUSH=1`
- Безопасность:
  - если задан `POCKETCODER_API_KEY`, daemon требует заголовок `x-api-key` для всех API вызовов
  - bot автоматически прокидывает этот ключ при наличии env переменной
  - bot поддерживает allowlist через `POCKETCODER_ALLOWED_USER_IDS` и `POCKETCODER_ALLOWED_CHAT_IDS`
  - disk guard: daemon отклоняет новые job, если свободное место ниже `POCKETCODER_MIN_FREE_DISK_MB`
- Engine adapter контракт:
  - `detect_capabilities`
  - `build_command`
  - `start_process`
  - `stream_events`
  - `send_input`
  - `cancel`
  - `summarize`
- Telegram bot команды:
  - `/start` (меню с inline-кнопками)
  - `/wizard` (state machine wizard: engine -> repo -> mode -> prompt -> timeout -> confirm)
  - `/cancelwizard`
  - `/create <engine> <repo> <mode> <prompt>`
  - `/jobs`
  - `/job <id>`
  - `/cancel <id>`
  - `/input <id> [text]`
  - inline-кнопки на списке/карточке job: open, refresh, cancel, send input
  - для `WAITING_INPUT` поддерживаются кнопки вариантов (`input_options`) + ручной ввод
  - для terminal статусов карточка показывает duration, stdout preview, commit и пути логов
  - wizard выбирает engines динамически через `GET /engines` (без локального хардкода)

## Codex Adapter

Engines `codex`, `cloudcode`, `opencode` подключены в daemon и доступны из API/wizard.

- По умолчанию команды:
  - SAFE: `codex exec --mode safe <prompt>`
  - YOLO: `codex exec --mode yolo <prompt>`
- Переопределение через env:
  - `POCKETCODER_CODEX_BIN`
  - `POCKETCODER_CODEX_SAFE_ARGS`
  - `POCKETCODER_CODEX_YOLO_ARGS`
  - `POCKETCODER_CODEX_SAFE_COMMAND_JSON`
  - `POCKETCODER_CODEX_YOLO_COMMAND_JSON`
  - `POCKETCODER_CODEX_REQUIRES_PTY`
  - `POCKETCODER_CODEX_SUPPORTS_NONINTERACTIVE`
  - `POCKETCODER_CODEX_SUPPORTS_YOLO`
  - `POCKETCODER_CODEX_SUPPORTS_JSON_EVENTS`
- Для `cloudcode` и `opencode` используется тот же шаблон env-переменных:
  - `POCKETCODER_<ENGINE>_BIN`
  - `POCKETCODER_<ENGINE>_SAFE_ARGS`
  - `POCKETCODER_<ENGINE>_YOLO_ARGS`
  - `POCKETCODER_<ENGINE>_SAFE_COMMAND_JSON`
  - `POCKETCODER_<ENGINE>_YOLO_COMMAND_JSON`
  - `POCKETCODER_<ENGINE>_REQUIRES_PTY`
  - `POCKETCODER_<ENGINE>_SUPPORTS_NONINTERACTIVE`
  - `POCKETCODER_<ENGINE>_SUPPORTS_YOLO`
  - `POCKETCODER_<ENGINE>_SUPPORTS_JSON_EVENTS`
  - где `<ENGINE>`: `CLOUDCODE` или `OPENCODE`
- Для lifecycle CLI (install/update):
  - `POCKETCODER_<ENGINE>_INSTALL_CMD`
  - `POCKETCODER_<ENGINE>_UPDATE_CMD`
  - `POCKETCODER_<ENGINE>_VERSION_ARGS`
  - в командах можно использовать `{bin}` как placeholder для текущего бинарника
- Автоматизация install/update в daemon:
  - `POCKETCODER_CLI_AUTO_INSTALL=1` — установить missing CLI при старте
  - `POCKETCODER_CLI_AUTO_UPDATE_INTERVAL_SECONDS=<N>` — авто-update всех CLI каждые `N` секунд

## Локальный запуск

```bash
python -m pip install -e '.[dev]'

# daemon
pocketcoder-daemon

# bot (в отдельном процессе)
export TELEGRAM_BOT_TOKEN=...
export POCKETCODER_DAEMON_URL=http://127.0.0.1:8080
pocketcoder-bot
```

## Docker Compose

```bash
export TELEGRAM_BOT_TOKEN=...
docker compose up --build
```

## Тесты

```bash
pytest -q
```
