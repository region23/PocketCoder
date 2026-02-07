PRD: PocketCoder: Telegram AI Coding Orchestrator

1. Обзор продукта

Инструмент представляет собой Telegram-бота, управляющего набором CLI-агентов кодогенерации, запущенных на VPS. Бот выступает control-plane для выполнения задач разработки (создание проектов, багфиксы, refactoring, коммиты, push в GitHub) без необходимости локального компьютера.

Система обеспечивает:
	•	выбор engine (Codex / CloudCode / OpenCode)
	•	выбор репозитория и контекста проекта
	•	запуск задач в YOLO или SAFE режиме
	•	интерактивный режим (non-YOLO) через Telegram
	•	управление жизненным циклом job
	•	хранение истории задач
	•	логирование stdout/stderr в отдельные файлы

2. Цели

Основная цель

Создать персональную production-нодe удалённой разработки, управляемую из Telegram.

Подцели
	•	автономная работа CLI-агентов 24/7
	•	возможность запуска задач с телефона
	•	параллельная работа над несколькими репозиториями
	•	управление job и их остановка
	•	история выполнения
	•	безопасное выполнение в прод-контуре

3. Scope

In Scope
	•	single-user использование
	•	отдельные репозитории
	•	VPS минимальной мощности
	•	engine adapters
	•	YOLO / SAFE режимы
	•	интерактивный wizard (non-YOLO)
	•	логирование
	•	история задач
	•	управление job

Out of Scope (MVP)
	•	web-интерфейс
	•	multi-user
	•	billing
	•	fine-grained resource scheduling
	•	advanced CI/CD интеграции

4. Основные сущности

4.1 Job

Сессия выполнения задачи агентом.

Содержит:
	•	id
	•	engine
	•	repo
	•	branch
	•	mode (YOLO / SAFE)
	•	status
	•	timestamps
	•	prompt
	•	stdout_log_path
	•	stderr_log_path
	•	artifacts

4.2 Workspace

Папка проекта на VPS.

/projects/<repo_name>

4.3 Engine

CLI-утилита кодогенерации.

Примеры:
	•	Codex
	•	CloudCode
	•	OpenCode

5. UX-флоу

5.1 Запуск job
	1.	Пользователь открывает бота
	2.	Выбирает:
	•	engine
	•	repo
	•	режим (YOLO / SAFE)
	3.	Вводит prompt
	4.	Бот:
	•	создаёт branch
	•	запускает job
	•	отвечает статусом запуска

5.2 Статус job

Отображается:
	•	started_at
	•	engine
	•	repo
	•	branch
	•	mode
	•	status

Обновляется по запросу.

5.3 Завершение

По окончании:
	•	финальный статус
	•	длительность
	•	stdout
	•	ссылка на лог
	•	commit / branch

5.4 Интерактивный wizard (non-YOLO)

Если CLI требует ввод:
	•	бот фиксирует событие input_required
	•	отправляет сообщение
	•	предлагает:
	•	кнопки вариантов
	•	ввод вручную
	•	ответ передаётся в stdin процесса

5.5 Cancel job

Кнопка:
	•	SIGTERM
	•	ожидание
	•	SIGKILL
	•	статус CANCELLED

6. Job lifecycle

CREATED
↓
STARTING
↓
RUNNING
↓
WAITING_INPUT (optional)
↓
COMPLETED / FAILED / CANCELLED / TIMEOUT

7. Ограничения
	•	1 активная job на репозиторий
	•	каждая job = новая ветка
	•	jobs могут выполняться до 24 часов

8. Логи

Для каждой job:

/logs/job-<id>.log

Требования:
	•	append-only
	•	ротация
	•	gzip по завершению

9. История задач

Хранение:
	•	sqlite

Данные:
	•	id
	•	engine
	•	repo
	•	prompt
	•	timestamps
	•	status
	•	branch
	•	commit
	•	log path

10. Архитектура

10.1 High-level

Система состоит из двух процессов:
	•	Telegram Bot (UI/control plane client)
	•	Runner Daemon (orchestration/runtime), работающий независимо от бота

Коммуникация Bot ↔ Daemon через локальный API (HTTP over localhost / Unix domain socket).

Daemon запускается в Docker-контейнере.

Telegram Bot
   ↓ (HTTP/Uds)
Control API (Daemon)
   ↓
Job Manager
   ↓
Execution Runner
   ↓
Engine Adapters
   ↓
CLI agents

Telegram Bot
↓
Control Layer
↓
Job Manager
↓
Execution Runner
↓
Engine Adapters
↓
CLI agents

## 11. Engine Adapter

Контракт:

- detect_capabilities()
- build_command()
- start_process()
- stream_events()
- send_input()
- cancel()
- summarize()

Capabilities:
- supports_json_events
- supports_noninteractive
- supports_yolo
- requires_pty


## 12. Execution Runner

Функции:
- запуск процесса в **отдельной process group** (убийство дерева процессов)
- PTY для интерактивных режимов
- управление stdin (ответы wizard)
- запись stdout/stderr в per-job log file
- контроль статуса/таймаутов
- интеграция с Git Manager (branch/commit/push)

### 12.1 Daemon lifecycle
- Daemon хранит состояния job в sqlite
- При рестарте daemon восстанавливает:
  - историю job
  - активные job помечает как `UNKNOWN/LOST` и предлагает пользователю ручную обработку (или пытается переattach, если возможно)

## 13. Безопасность

Требования production-уровня:

- запуск без shell
- argv-команды
- изоляция process group
- хранение ключей GitHub в env
- контроль доступов
- контроль диска


## 14. Git-стратегия

- новая ветка на каждую job
- push в origin
- PR опционально


## 15. Data model (sqlite)

Tables:

jobs
engines
repos
artifacts


## 16. Нефункциональные требования

- устойчивость к перезапуску
- jobs > 24h
- минимальная нагрузка на VPS
- fault-tolerance


## 17. Стек (MVP)

- Python 3.12+
- Telegram SDK: python-telegram-bot (или aiogram)
- Runner Daemon: FastAPI (HTTP) или aiohttp (lightweight)
- asyncio
- sqlite
- subprocess + pty
- Docker + docker-compose
- systemd (опционально) для автозапуска docker-compose

## 18. Roadmap

### Phase 1
- базовый бот
- запуск CLI
- логирование
- история

### Phase 2
- engine adapters
- wizard
- cancel

### Phase 3
- structured output parsing
- улучшения безопасности
- расширение engines


## 19. Риски

- runaway jobs
- disk overflow
- утечка ключей
- зависшие процессы


## 20. Success criteria

- запуск job из Telegram
- работа 24/7
- управление задачами
- безопасное выполнение
- стабильная история

---

проект: PocketCoder
daemon: pocketcoder-daemon
bot: pocketcoder-bot
cli (если появится): pc
docker:
pocketcoder/daemon
pocketcoder/bot
