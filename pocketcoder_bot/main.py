from __future__ import annotations

import os
from typing import Sequence

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

from pocketcoder_bot.daemon_client import DaemonAPIError, DaemonClient


def _daemon_client() -> DaemonClient:
    url = os.getenv("POCKETCODER_DAEMON_URL", "http://127.0.0.1:8080")
    return DaemonClient(base_url=url)


def _help_text() -> str:
    return (
        "PocketCoder bot commands:\n"
        "/create <engine> <repo> <mode> <prompt>\n"
        "/jobs\n"
        "/job <id>\n"
        "/cancel <id>\n"
        "/input <id> <text>"
    )


def _format_job(job: dict) -> str:
    return (
        f"job #{job['id']} | {job['status']}\n"
        f"engine={job['engine']} repo={job['repo']} mode={job['mode']}\n"
        f"branch={job['branch']}\n"
        f"created={job['created_at']}\n"
        f"started={job.get('started_at')} finished={job.get('finished_at')}\n"
        f"input_prompt={job.get('input_prompt')}"
    )


async def start_command(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(_help_text())


async def help_command(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message:
        await update.message.reply_text(_help_text())


async def create_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if len(context.args) < 4:
        await update.message.reply_text("Usage: /create <engine> <repo> <mode> <prompt>")
        return
    engine, repo, mode = context.args[0], context.args[1], context.args[2]
    prompt = " ".join(context.args[3:])
    try:
        job = await _daemon_client().create_job(
            engine=engine,
            repo=repo,
            mode=mode.upper(),
            prompt=prompt,
        )
    except DaemonAPIError as exc:
        await update.message.reply_text(f"Create failed: {exc}")
        return
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
    lines: list[str] = []
    for job in jobs[:10]:
        lines.append(f"#{job['id']} {job['status']} {job['engine']} {job['repo']}")
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
    await update.message.reply_text(_format_job(job))


async def input_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /input <id> <text>")
        return
    job_id = _parse_job_id(context.args[:1])
    if job_id is None:
        await update.message.reply_text("Usage: /input <id> <text>")
        return
    text = " ".join(context.args[1:])
    try:
        job = await _daemon_client().send_input(job_id, text)
    except DaemonAPIError as exc:
        await update.message.reply_text(f"Input failed: {exc}")
        return
    await update.message.reply_text(_format_job(job))


def run() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

    app = ApplicationBuilder().token(token).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("create", create_command))
    app.add_handler(CommandHandler("jobs", jobs_command))
    app.add_handler(CommandHandler("job", job_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("input", input_command))
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    run()

