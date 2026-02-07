# PocketCoder

`PocketCoder` состоит из двух процессов:

- `pocketcoder-daemon` (FastAPI + runner + sqlite)
- `pocketcoder-bot` (Telegram control-plane клиент)

## Реализовано сейчас

- Daemon API:
  - `POST /jobs`
  - `GET /jobs`
  - `GET /jobs/{id}`
  - `POST /jobs/{id}/cancel`
  - `POST /jobs/{id}/input`
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
  - timeout и cancel (SIGTERM/SIGKILL)
  - запись stdout/stderr в отдельные файлы
  - gzip логов по завершению + регистрация логов как artifacts
- Git (если workspace уже git-репозиторий):
  - checkout новой ветки `pc/<repo>/<timestamp>` для job
  - best-effort commit итоговых изменений
  - сохранение `commit_hash` в job
  - optional push при `POCKETCODER_GIT_PUSH=1`
- Безопасность:
  - если задан `POCKETCODER_API_KEY`, daemon требует заголовок `x-api-key` для всех API вызовов
  - bot автоматически прокидывает этот ключ при наличии env переменной
- Engine adapter контракт:
  - `detect_capabilities`
  - `build_command`
  - `start_process`
  - `stream_events`
  - `send_input`
  - `cancel`
  - `summarize`
- Telegram bot команды:
  - `/create <engine> <repo> <mode> <prompt>`
  - `/jobs`
  - `/job <id>`
  - `/cancel <id>`
  - `/input <id> <text>`

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
