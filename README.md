# PocketCoder

MVP реализации `pocketcoder-daemon` из PRD с подходом TDD.

## Что реализовано

- FastAPI daemon с API:
  - `POST /jobs` — создать job
  - `GET /jobs` — список job
  - `GET /jobs/{id}` — детали job
  - `POST /jobs/{id}/cancel` — отмена job
- SQLite-хранилище истории job.
- Базовые engine adapters (`mock`, `sleepy`) с контрактом capabilities/build_command.
- Execution Runner:
  - запуск через `subprocess` без shell
  - отдельная process group
  - cancel через SIGTERM/SIGKILL
  - логирование per-job и gzip логов по завершению
- Восстановление после рестарта: активные job помечаются как `LOST`.
- Ограничение: не более одной активной job на репозиторий.

## Запуск

```bash
python -m pip install -e '.[dev]'
python -m pocketcoder_daemon.main
```

## Тесты

```bash
pytest -q
```
