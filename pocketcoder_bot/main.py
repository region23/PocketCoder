from __future__ import annotations

from datetime import datetime
from enum import IntEnum
import os
from typing import Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationHandlerStop,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from pocketcoder_bot.access import AccessPolicy, is_allowed
from pocketcoder_bot.daemon_client import DaemonAPIError, DaemonClient

DEFAULT_ENGINES = ("codex", "cloudcode", "opencode")
SUPPORTED_MODES = ("YOLO", "SAFE")
WIZARD_DATA_KEY = "wizard_draft"
PENDING_INPUT_JOB_KEY = "pending_input_job_id"
WIZARD_ENGINES_KEY = "wizard_engines"
JOB_INPUT_OPTIONS_KEY = "job_input_options_cache"
JOB_STATUS_CACHE_KEY = "job_status_cache"
MAX_TIMEOUT_SECONDS = 24 * 60 * 60


class WizardState(IntEnum):
    ENGINE = 1
    REPO = 2
    MODE = 3
    PROMPT = 4
    TIMEOUT = 5
    CONFIRM = 6


def _daemon_client() -> DaemonClient:
    uds_path = os.getenv("POCKETCODER_DAEMON_UDS")
    url = os.getenv("POCKETCODER_DAEMON_URL", "http://127.0.0.1:8080")
    if uds_path:
        return DaemonClient(base_url="http://daemon", uds_path=uds_path)
    return DaemonClient(base_url=url)


def _help_text() -> str:
    return (
        "PocketCoder bot commands:\n"
        "/wizard - interactive create flow\n"
        "/cancelwizard - abort wizard\n"
        "/create <engine> <repo> <mode> <prompt>\n"
        "/jobs\n"
        "/lost\n"
        "/job <id>\n"
        "/cancel <id>\n"
        "/input <id> [text]"
    )


def _actor_ids(update: Update) -> tuple[int | None, int | None]:
    user_id = update.effective_user.id if update.effective_user else None
    chat_id = update.effective_chat.id if update.effective_chat else None
    return user_id, chat_id


def _parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    whole = max(0, int(seconds))
    hours, rem = divmod(whole, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _job_duration_seconds(job: dict) -> float | None:
    started = _parse_dt(job.get("started_at"))
    finished = _parse_dt(job.get("finished_at"))
    if started and finished:
        return (finished - started).total_seconds()
    created = _parse_dt(job.get("created_at"))
    if created and finished:
        return (finished - created).total_seconds()
    return None


def _is_terminal(status: str | None) -> bool:
    return status in {"COMPLETED", "FAILED", "CANCELLED", "TIMEOUT", "LOST"}


def _job_input_options(job: dict) -> list[str]:
    raw = job.get("input_options")
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for item in raw:
        if isinstance(item, str):
            result.append(item)
    return result


def _format_job(job: dict) -> str:
    status = job.get("status")
    lines = [
        f"job #{job['id']} | {status}",
        f"engine={job.get('engine')} repo={job.get('repo')} mode={job.get('mode')}",
        f"branch={job.get('branch')}",
        f"created={job.get('created_at')}",
        f"started={job.get('started_at')} finished={job.get('finished_at')}",
    ]

    if status == "WAITING_INPUT":
        lines.append(f"input_prompt={job.get('input_prompt')}")
        options = _job_input_options(job)
        if options:
            lines.append(f"input_options={', '.join(options)}")

    if _is_terminal(status):
        lines.append(f"duration={_format_duration(_job_duration_seconds(job))}")
        preview = job.get("stdout_preview")
        if isinstance(preview, str) and preview:
            lines.append(f"stdout_preview={preview}")
        commit = job.get("commit_hash")
        if isinstance(commit, str) and commit:
            lines.append(f"commit={commit}")
        artifacts = job.get("artifacts")
        if isinstance(artifacts, list) and artifacts:
            rendered = [item for item in artifacts if isinstance(item, str)]
            if rendered:
                lines.append(f"logs={', '.join(rendered[:2])}")

    return "\n".join(lines)


def _wizard_engine_keyboard(engines: Sequence[str]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(engine, callback_data=f"wiz:engine:{engine}")]
        for engine in engines
    ]
    rows.append([InlineKeyboardButton("Cancel", callback_data="wiz:confirm:cancel")])
    return InlineKeyboardMarkup(rows)


def _wizard_mode_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(mode, callback_data=f"wiz:mode:{mode}")]
        for mode in SUPPORTED_MODES
    ]
    rows.append([InlineKeyboardButton("Cancel", callback_data="wiz:confirm:cancel")])
    return InlineKeyboardMarkup(rows)


def _wizard_timeout_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("No timeout", callback_data="wiz:timeout:none"),
                InlineKeyboardButton("30m", callback_data="wiz:timeout:1800"),
            ],
            [
                InlineKeyboardButton("2h", callback_data="wiz:timeout:7200"),
                InlineKeyboardButton("Custom", callback_data="wiz:timeout:custom"),
            ],
            [InlineKeyboardButton("Cancel", callback_data="wiz:confirm:cancel")],
        ]
    )


def _wizard_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Create job", callback_data="wiz:confirm:create")],
            [InlineKeyboardButton("Restart wizard", callback_data="wiz:confirm:restart")],
            [InlineKeyboardButton("Cancel", callback_data="wiz:confirm:cancel")],
        ]
    )


def _jobs_keyboard(jobs: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                f"#{job['id']} {job['status']} {job['repo']}",
                callback_data=f"job:open:{job['id']}",
            )
        ]
        for job in jobs[:10]
    ]
    rows.append([InlineKeyboardButton("Refresh list", callback_data="job:list")])
    rows.append([InlineKeyboardButton("New job wizard", callback_data="menu:wizard")])
    return InlineKeyboardMarkup(rows)


def _job_actions_keyboard(job: dict) -> InlineKeyboardMarkup:
    job_id = job["id"]
    rows = [
        [
            InlineKeyboardButton("Refresh", callback_data=f"job:action:{job_id}:refresh"),
            InlineKeyboardButton("Cancel job", callback_data=f"job:action:{job_id}:cancel"),
        ]
    ]
    if job.get("status") == "WAITING_INPUT":
        options = _job_input_options(job)
        for idx, option in enumerate(options[:6]):
            rows.append(
                [
                    InlineKeyboardButton(
                        f"Option: {option}",
                        callback_data=f"job:inputopt:{job_id}:{idx}",
                    )
                ]
            )
        rows.append([InlineKeyboardButton("Send manual input", callback_data=f"job:action:{job_id}:input")])
    rows.append([InlineKeyboardButton("Back to jobs", callback_data="job:list")])
    return InlineKeyboardMarkup(rows)


def _new_wizard_draft() -> dict[str, object]:
    return {
        "engine": None,
        "repo": None,
        "mode": None,
        "prompt": None,
        "timeout_seconds": None,
        "await_custom_timeout": False,
    }


def _wizard_draft(context: ContextTypes.DEFAULT_TYPE) -> dict[str, object]:
    draft = context.user_data.get(WIZARD_DATA_KEY)
    if not isinstance(draft, dict):
        draft = _new_wizard_draft()
        context.user_data[WIZARD_DATA_KEY] = draft
    return draft


def _clear_wizard(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop(WIZARD_DATA_KEY, None)
    context.user_data.pop(WIZARD_ENGINES_KEY, None)


def _cache_job_input_options(context: ContextTypes.DEFAULT_TYPE, job: dict) -> None:
    raw_map = context.user_data.get(JOB_INPUT_OPTIONS_KEY)
    if not isinstance(raw_map, dict):
        raw_map = {}
        context.user_data[JOB_INPUT_OPTIONS_KEY] = raw_map
    job_id = job.get("id")
    if not isinstance(job_id, int):
        return
    options = _job_input_options(job)
    if options:
        raw_map[job_id] = options
    elif job_id in raw_map:
        raw_map.pop(job_id, None)


def _cached_job_input_options(context: ContextTypes.DEFAULT_TYPE, job_id: int) -> list[str]:
    raw_map = context.user_data.get(JOB_INPUT_OPTIONS_KEY)
    if not isinstance(raw_map, dict):
        return []
    options = raw_map.get(job_id)
    if not isinstance(options, list):
        return []
    result: list[str] = []
    for item in options:
        if isinstance(item, str):
            result.append(item)
    return result


def _job_requester_chat_id(job: dict) -> int | None:
    chat_id = job.get("requester_chat_id")
    if isinstance(chat_id, int):
        return chat_id
    return None


def _status_cache(context: ContextTypes.DEFAULT_TYPE) -> dict[int, str]:
    raw = context.application.bot_data.get(JOB_STATUS_CACHE_KEY)
    if isinstance(raw, dict):
        normalized: dict[int, str] = {}
        for key, value in raw.items():
            if isinstance(key, int) and isinstance(value, str):
                normalized[key] = value
        if normalized != raw:
            context.application.bot_data[JOB_STATUS_CACHE_KEY] = normalized
        return normalized
    cache: dict[int, str] = {}
    context.application.bot_data[JOB_STATUS_CACHE_KEY] = cache
    return cache


async def _notify_waiting_input(context: ContextTypes.DEFAULT_TYPE, job: dict) -> None:
    chat_id = _job_requester_chat_id(job)
    if chat_id is None:
        return
    prompt = job.get("input_prompt")
    prompt_text = prompt if isinstance(prompt, str) and prompt else "Input required"
    text = f"Job #{job['id']} waiting input: {prompt_text}\n\n{_format_job(job)}"
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=_job_actions_keyboard(job),
        )
    except Exception:
        return


async def _notify_lost_job(context: ContextTypes.DEFAULT_TYPE, job: dict) -> None:
    chat_id = _job_requester_chat_id(job)
    if chat_id is None:
        return
    text = (
        f"Job #{job['id']} marked as LOST after daemon restart.\n"
        "Check job details and continue manually if needed.\n\n"
        f"{_format_job(job)}"
    )
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=_job_actions_keyboard(job),
        )
    except Exception:
        return


async def poll_job_updates(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        jobs = await _daemon_client().list_jobs()
    except DaemonAPIError:
        return
    cache = _status_cache(context)
    seen: set[int] = set()
    for job in jobs:
        job_id = job.get("id")
        status = job.get("status")
        if not isinstance(job_id, int) or not isinstance(status, str):
            continue
        seen.add(job_id)
        previous = cache.get(job_id)
        if status == "WAITING_INPUT" and previous != "WAITING_INPUT":
            await _notify_waiting_input(context, job)
        if status == "LOST" and previous != "LOST":
            await _notify_lost_job(context, job)
        cache[job_id] = status
    stale_ids = [job_id for job_id in cache if job_id not in seen]
    for job_id in stale_ids:
        cache.pop(job_id, None)


def _draft_summary(draft: dict[str, object]) -> str:
    timeout_seconds = draft.get("timeout_seconds")
    timeout_text = "none" if timeout_seconds is None else str(timeout_seconds)
    return (
        "Wizard summary:\n"
        f"engine={draft.get('engine')}\n"
        f"repo={draft.get('repo')}\n"
        f"mode={draft.get('mode')}\n"
        f"prompt={draft.get('prompt')}\n"
        f"timeout_seconds={timeout_text}"
    )


def _parse_timeout_seconds(text: str) -> float | None:
    try:
        value = float(text)
    except ValueError:
        return None
    if value <= 0 or value > MAX_TIMEOUT_SECONDS:
        return None
    return value


def _saved_wizard_engines(context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    raw = context.user_data.get(WIZARD_ENGINES_KEY)
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return raw
    return list(DEFAULT_ENGINES)


async def _load_wizard_engines(context: ContextTypes.DEFAULT_TYPE) -> list[str]:
    try:
        payload = await _daemon_client().list_engines()
        engines = [
            item.get("name")
            for item in payload
            if isinstance(item, dict) and isinstance(item.get("name"), str)
        ]
    except DaemonAPIError:
        engines = []
    if not engines:
        engines = list(DEFAULT_ENGINES)
    context.user_data[WIZARD_ENGINES_KEY] = engines
    return engines


async def start_command(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(
            _help_text(),
            reply_markup=InlineKeyboardMarkup(
                [
                    [InlineKeyboardButton("New job wizard", callback_data="menu:wizard")],
                    [InlineKeyboardButton("Show jobs", callback_data="menu:jobs")],
                ]
            ),
        )


async def help_command(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(_help_text())


async def cancel_wizard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _clear_wizard(context)
    if update.message:
        await update.message.reply_text("Wizard state cleared")


async def create_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if len(context.args) < 4:
        await update.message.reply_text("Usage: /create <engine> <repo> <mode> <prompt>")
        return
    engine, repo, mode = context.args[0], context.args[1], context.args[2]
    prompt = " ".join(context.args[3:])
    requester_user_id, requester_chat_id = _actor_ids(update)
    try:
        job = await _daemon_client().create_job(
            engine=engine,
            repo=repo,
            mode=mode.upper(),
            prompt=prompt,
            requester_user_id=requester_user_id,
            requester_chat_id=requester_chat_id,
        )
    except DaemonAPIError as exc:
        await update.message.reply_text(f"Create failed: {exc}")
        return
    _cache_job_input_options(context, job)
    await update.message.reply_text(_format_job(job))


async def jobs_command(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    try:
        jobs = await _daemon_client().list_jobs()
    except DaemonAPIError as exc:
        await update.message.reply_text(f"List failed: {exc}")
        return
    if not jobs:
        await update.message.reply_text("No jobs yet")
        return
    await update.message.reply_text("Jobs:", reply_markup=_jobs_keyboard(jobs))


async def lost_jobs_command(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    try:
        jobs = await _daemon_client().list_jobs()
    except DaemonAPIError as exc:
        await update.message.reply_text(f"List failed: {exc}")
        return
    lost = [job for job in jobs if job.get("status") == "LOST"]
    if not lost:
        await update.message.reply_text("No LOST jobs")
        return
    lines = ["LOST jobs:"]
    for job in lost[:10]:
        lines.append(f"#{job['id']} repo={job.get('repo')} branch={job.get('branch')}")
    await update.message.reply_text("\n".join(lines))


def _parse_job_id(args: Sequence[str]) -> int | None:
    if not args:
        return None
    try:
        return int(args[0])
    except ValueError:
        return None


async def job_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    job_id = _parse_job_id(context.args)
    if job_id is None:
        await update.message.reply_text("Usage: /job <id>")
        return
    try:
        job = await _daemon_client().get_job(job_id)
    except DaemonAPIError as exc:
        await update.message.reply_text(f"Get failed: {exc}")
        return
    _cache_job_input_options(context, job)
    await update.message.reply_text(_format_job(job))


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    job_id = _parse_job_id(context.args)
    if job_id is None:
        await update.message.reply_text("Usage: /cancel <id>")
        return
    try:
        job = await _daemon_client().cancel_job(job_id)
    except DaemonAPIError as exc:
        await update.message.reply_text(f"Cancel failed: {exc}")
        return
    _cache_job_input_options(context, job)
    await update.message.reply_text(_format_job(job))


async def input_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if len(context.args) < 1:
        await update.message.reply_text("Usage: /input <id> [text]")
        return
    job_id = _parse_job_id(context.args[:1])
    if job_id is None:
        await update.message.reply_text("Usage: /input <id> [text]")
        return
    if len(context.args) == 1:
        try:
            job = await _daemon_client().get_job(job_id)
        except DaemonAPIError as exc:
            await update.message.reply_text(f"Get failed: {exc}")
            return
        _cache_job_input_options(context, job)
        context.user_data[PENDING_INPUT_JOB_KEY] = job_id
        prompt = (
            f"Send one text message for job #{job_id}. It will be forwarded to daemon stdin."
        )
        if job.get("status") == "WAITING_INPUT":
            prompt = (
                f"Job #{job_id} is waiting input: {job.get('input_prompt')}\n"
                "Use option buttons below or send manual text."
            )
        await update.message.reply_text(prompt, reply_markup=_job_actions_keyboard(job))
        return
    text = " ".join(context.args[1:]).strip()
    if not text:
        await update.message.reply_text("Input text cannot be empty")
        return
    try:
        job = await _daemon_client().send_input(job_id, text)
    except DaemonAPIError as exc:
        await update.message.reply_text(f"Input failed: {exc}")
        return
    _cache_job_input_options(context, job)
    await update.message.reply_text(_format_job(job))


async def pending_input_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    pending_job_id = context.user_data.get(PENDING_INPUT_JOB_KEY)
    if not isinstance(pending_job_id, int):
        return
    text = update.message.text.strip()
    if not text:
        await update.message.reply_text("Input text cannot be empty")
        return
    try:
        job = await _daemon_client().send_input(pending_job_id, text)
    except DaemonAPIError as exc:
        await update.message.reply_text(f"Input failed: {exc}")
        return
    context.user_data.pop(PENDING_INPUT_JOB_KEY, None)
    _cache_job_input_options(context, job)
    await update.message.reply_text(_format_job(job), reply_markup=_job_actions_keyboard(job))


async def menu_callback(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()
    if query.data == "menu:jobs":
        try:
            jobs = await _daemon_client().list_jobs()
        except DaemonAPIError as exc:
            await query.edit_message_text(f"List failed: {exc}")
            return
        if not jobs:
            await query.edit_message_text("No jobs yet")
            return
        await query.edit_message_text("Jobs:", reply_markup=_jobs_keyboard(jobs))


async def jobs_list_callback(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return
    await query.answer()
    try:
        jobs = await _daemon_client().list_jobs()
    except DaemonAPIError as exc:
        await query.edit_message_text(f"List failed: {exc}")
        return
    if not jobs:
        await query.edit_message_text("No jobs yet")
        return
    await query.edit_message_text("Jobs:", reply_markup=_jobs_keyboard(jobs))


async def job_open_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()
    _, _, raw_job_id = query.data.split(":", maxsplit=2)
    try:
        job_id = int(raw_job_id)
    except ValueError:
        await query.edit_message_text("Invalid job id")
        return
    try:
        job = await _daemon_client().get_job(job_id)
    except DaemonAPIError as exc:
        await query.edit_message_text(f"Get failed: {exc}")
        return
    _cache_job_input_options(context, job)
    await query.edit_message_text(_format_job(job), reply_markup=_job_actions_keyboard(job))


async def job_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()
    parts = query.data.split(":")
    if len(parts) != 4:
        await query.edit_message_text("Invalid action")
        return
    _, _, raw_job_id, action = parts
    try:
        job_id = int(raw_job_id)
    except ValueError:
        await query.edit_message_text("Invalid job id")
        return

    if action == "refresh":
        try:
            job = await _daemon_client().get_job(job_id)
        except DaemonAPIError as exc:
            await query.edit_message_text(f"Get failed: {exc}")
            return
        _cache_job_input_options(context, job)
        await query.edit_message_text(_format_job(job), reply_markup=_job_actions_keyboard(job))
        return

    if action == "cancel":
        try:
            job = await _daemon_client().cancel_job(job_id)
        except DaemonAPIError as exc:
            await query.edit_message_text(f"Cancel failed: {exc}")
            return
        _cache_job_input_options(context, job)
        await query.edit_message_text(_format_job(job), reply_markup=_job_actions_keyboard(job))
        return

    if action == "input":
        context.user_data[PENDING_INPUT_JOB_KEY] = job_id
        if query.message:
            await query.message.reply_text(
                f"Send one text message for job #{job_id}. It will be forwarded to daemon stdin."
            )
        else:
            await query.edit_message_text(
                f"Send one text message for job #{job_id}. It will be forwarded to daemon stdin."
            )
        return

    await query.edit_message_text("Unsupported action")


async def job_input_option_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()
    parts = query.data.split(":")
    if len(parts) != 4:
        await query.edit_message_text("Invalid option action")
        return
    _, _, raw_job_id, raw_idx = parts
    try:
        job_id = int(raw_job_id)
        idx = int(raw_idx)
    except ValueError:
        await query.edit_message_text("Invalid option action")
        return

    options = _cached_job_input_options(context, job_id)
    if not options:
        try:
            job = await _daemon_client().get_job(job_id)
        except DaemonAPIError as exc:
            await query.edit_message_text(f"Get failed: {exc}")
            return
        _cache_job_input_options(context, job)
        options = _job_input_options(job)
    if idx < 0 or idx >= len(options):
        await query.edit_message_text("Input option is no longer available. Refresh job.")
        return
    selected = options[idx]

    try:
        job = await _daemon_client().send_input(job_id, selected)
    except DaemonAPIError as exc:
        await query.edit_message_text(f"Input failed: {exc}")
        return
    _cache_job_input_options(context, job)
    await query.edit_message_text(_format_job(job), reply_markup=_job_actions_keyboard(job))


async def wizard_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data[WIZARD_DATA_KEY] = _new_wizard_draft()
    engines = await _load_wizard_engines(context)
    if update.message:
        await update.message.reply_text(
            "Wizard started. Step 1/6: choose engine",
            reply_markup=_wizard_engine_keyboard(engines),
        )
        return WizardState.ENGINE
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(
            "Wizard started. Step 1/6: choose engine",
            reply_markup=_wizard_engine_keyboard(engines),
        )
    return WizardState.ENGINE


async def wizard_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    _clear_wizard(context)
    if update.message:
        await update.message.reply_text("Wizard cancelled")
        return ConversationHandler.END
    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text("Wizard cancelled")
    return ConversationHandler.END


async def wizard_pick_engine(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.data is None:
        return WizardState.ENGINE
    await query.answer()
    _, _, engine = query.data.split(":", maxsplit=2)
    if engine not in _saved_wizard_engines(context):
        await query.edit_message_text("Unknown engine. Choose from list.")
        return WizardState.ENGINE
    draft = _wizard_draft(context)
    draft["engine"] = engine
    await query.edit_message_text("Step 2/6: send repository name as plain text")
    return WizardState.REPO


async def wizard_set_repo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return WizardState.REPO
    repo = update.message.text.strip()
    if not repo:
        await update.message.reply_text("Repository name cannot be empty")
        return WizardState.REPO
    draft = _wizard_draft(context)
    draft["repo"] = repo
    await update.message.reply_text(
        "Step 3/6: choose mode",
        reply_markup=_wizard_mode_keyboard(),
    )
    return WizardState.MODE


async def wizard_pick_mode(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.data is None:
        return WizardState.MODE
    await query.answer()
    _, _, mode = query.data.split(":", maxsplit=2)
    if mode not in SUPPORTED_MODES:
        await query.edit_message_text("Unknown mode. Choose from list.")
        return WizardState.MODE
    draft = _wizard_draft(context)
    draft["mode"] = mode
    await query.edit_message_text("Step 4/6: send prompt text")
    return WizardState.PROMPT


async def wizard_set_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return WizardState.PROMPT
    prompt = update.message.text.strip()
    if not prompt:
        await update.message.reply_text("Prompt cannot be empty")
        return WizardState.PROMPT
    draft = _wizard_draft(context)
    draft["prompt"] = prompt
    await update.message.reply_text(
        "Step 5/6: choose timeout",
        reply_markup=_wizard_timeout_keyboard(),
    )
    return WizardState.TIMEOUT


async def wizard_pick_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.data is None:
        return WizardState.TIMEOUT
    await query.answer()
    _, _, raw_value = query.data.split(":", maxsplit=2)
    draft = _wizard_draft(context)
    if raw_value == "custom":
        draft["await_custom_timeout"] = True
        await query.edit_message_text(
            f"Send timeout in seconds (1..{MAX_TIMEOUT_SECONDS})",
        )
        return WizardState.TIMEOUT
    draft["await_custom_timeout"] = False
    if raw_value == "none":
        draft["timeout_seconds"] = None
    else:
        parsed = _parse_timeout_seconds(raw_value)
        if parsed is None:
            await query.edit_message_text(
                "Invalid timeout selection",
                reply_markup=_wizard_timeout_keyboard(),
            )
            return WizardState.TIMEOUT
        draft["timeout_seconds"] = parsed
    await query.edit_message_text(
        f"Step 6/6: confirm\n\n{_draft_summary(draft)}",
        reply_markup=_wizard_confirm_keyboard(),
    )
    return WizardState.CONFIRM


async def wizard_set_custom_timeout(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message:
        return WizardState.TIMEOUT
    draft = _wizard_draft(context)
    awaiting = bool(draft.get("await_custom_timeout"))
    if not awaiting:
        await update.message.reply_text(
            "Use timeout buttons first",
            reply_markup=_wizard_timeout_keyboard(),
        )
        return WizardState.TIMEOUT
    parsed = _parse_timeout_seconds(update.message.text.strip())
    if parsed is None:
        await update.message.reply_text(
            f"Invalid timeout. Send seconds in range 1..{MAX_TIMEOUT_SECONDS}",
        )
        return WizardState.TIMEOUT
    draft["timeout_seconds"] = parsed
    draft["await_custom_timeout"] = False
    await update.message.reply_text(
        f"Step 6/6: confirm\n\n{_draft_summary(draft)}",
        reply_markup=_wizard_confirm_keyboard(),
    )
    return WizardState.CONFIRM


async def wizard_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if query is None or query.data is None:
        return WizardState.CONFIRM
    await query.answer()
    _, _, action = query.data.split(":", maxsplit=2)
    if action == "cancel":
        return await wizard_cancel(update, context)
    if action == "restart":
        context.user_data[WIZARD_DATA_KEY] = _new_wizard_draft()
        engines = await _load_wizard_engines(context)
        await query.edit_message_text(
            "Wizard restarted. Step 1/6: choose engine",
            reply_markup=_wizard_engine_keyboard(engines),
        )
        return WizardState.ENGINE
    if action != "create":
        await query.edit_message_text("Unsupported wizard action")
        return WizardState.CONFIRM

    draft = _wizard_draft(context)
    engine = draft.get("engine")
    repo = draft.get("repo")
    mode = draft.get("mode")
    prompt = draft.get("prompt")
    if not isinstance(engine, str) or not isinstance(repo, str):
        await query.edit_message_text(
            "Wizard data incomplete. Restart wizard.",
            reply_markup=_wizard_confirm_keyboard(),
        )
        return WizardState.CONFIRM
    if not isinstance(mode, str) or not isinstance(prompt, str):
        await query.edit_message_text(
            "Wizard data incomplete. Restart wizard.",
            reply_markup=_wizard_confirm_keyboard(),
        )
        return WizardState.CONFIRM

    timeout_value = draft.get("timeout_seconds")
    timeout_seconds = timeout_value if isinstance(timeout_value, float) else None
    requester_user_id, requester_chat_id = _actor_ids(update)
    try:
        job = await _daemon_client().create_job(
            engine=engine,
            repo=repo,
            mode=mode,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            requester_user_id=requester_user_id,
            requester_chat_id=requester_chat_id,
        )
    except DaemonAPIError as exc:
        await query.edit_message_text(
            f"Create failed: {exc}\n\n{_draft_summary(draft)}",
            reply_markup=_wizard_confirm_keyboard(),
        )
        return WizardState.CONFIRM
    _clear_wizard(context)
    _cache_job_input_options(context, job)
    await query.edit_message_text(_format_job(job), reply_markup=_job_actions_keyboard(job))
    return ConversationHandler.END


def run() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    access_policy = AccessPolicy.from_env()

    async def access_guard(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
        user_id, chat_id = _actor_ids(update)
        if is_allowed(access_policy, user_id=user_id, chat_id=chat_id):
            return
        if update.callback_query is not None:
            await update.callback_query.answer("Access denied", show_alert=True)
        elif update.message is not None:
            await update.message.reply_text("Access denied")
        raise ApplicationHandlerStop

    app = ApplicationBuilder().token(token).build()
    app.add_handler(TypeHandler(Update, access_guard), group=-1)
    wizard_handler = ConversationHandler(
        entry_points=[
            CommandHandler("wizard", wizard_start),
            CallbackQueryHandler(wizard_start, pattern=r"^menu:wizard$"),
        ],
        states={
            WizardState.ENGINE: [
                CallbackQueryHandler(wizard_pick_engine, pattern=r"^wiz:engine:[^:]+$")
            ],
            WizardState.REPO: [MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_set_repo)],
            WizardState.MODE: [
                CallbackQueryHandler(wizard_pick_mode, pattern=r"^wiz:mode:[^:]+$")
            ],
            WizardState.PROMPT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_set_prompt)
            ],
            WizardState.TIMEOUT: [
                CallbackQueryHandler(wizard_pick_timeout, pattern=r"^wiz:timeout:[^:]+$"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, wizard_set_custom_timeout),
            ],
            WizardState.CONFIRM: [
                CallbackQueryHandler(wizard_confirm, pattern=r"^wiz:confirm:[^:]+$")
            ],
        },
        fallbacks=[
            CommandHandler("cancelwizard", wizard_cancel),
            CallbackQueryHandler(wizard_cancel, pattern=r"^wiz:confirm:cancel$"),
        ],
        allow_reentry=True,
    )

    app.add_handler(wizard_handler)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("cancelwizard", cancel_wizard_command))
    app.add_handler(CommandHandler("create", create_command))
    app.add_handler(CommandHandler("jobs", jobs_command))
    app.add_handler(CommandHandler("lost", lost_jobs_command))
    app.add_handler(CommandHandler("job", job_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("input", input_command))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:jobs$"))
    app.add_handler(CallbackQueryHandler(jobs_list_callback, pattern=r"^job:list$"))
    app.add_handler(CallbackQueryHandler(job_open_callback, pattern=r"^job:open:\d+$"))
    app.add_handler(CallbackQueryHandler(job_input_option_callback, pattern=r"^job:inputopt:\d+:\d+$"))
    app.add_handler(
        CallbackQueryHandler(job_action_callback, pattern=r"^job:action:\d+:[a-z]+$")
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, pending_input_text))
    watch_interval = float(os.getenv("POCKETCODER_BOT_WATCH_INTERVAL_SECONDS", "5"))
    if app.job_queue is not None and watch_interval > 0:
        app.job_queue.run_repeating(
            poll_job_updates,
            interval=watch_interval,
            first=watch_interval,
        )
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    run()
