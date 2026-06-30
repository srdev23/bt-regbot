from __future__ import annotations

import logging
from collections.abc import Callable
from functools import wraps
from typing import Any

from telegram import Update
from telegram.ext import ContextTypes

from sn_reg_bot.handlers.common import access_service, is_bot_owner, settings, user_id
from sn_reg_bot.handlers.tg_helpers import answer_callback

logger = logging.getLogger(__name__)

_PUBLIC_COMMANDS = frozenset({"/start"})


def _command_name(update: Update) -> str | None:
    message = update.effective_message
    if message is None or not message.text or not message.text.startswith("/"):
        return None
    token = message.text.split()[0]
    if "@" in token:
        token = token.split("@", 1)[0]
    return token.lower()


async def check_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Return True if the update may proceed to the target handler."""
    if update.effective_user is None:
        return True

    if settings(context).bot_owner_id is None:
        return True

    uid = user_id(update)
    if is_bot_owner(context, uid):
        record = await access_service(context).get_user(uid)
        if record is not None and not record.is_approved():
            await access_service(context).approve_user(uid)
        return True

    cmd = _command_name(update)
    if cmd in _PUBLIC_COMMANDS:
        return True

    access = access_service(context)
    record = await access.get_user(uid)
    if record is None:
        if update.effective_message:
            await update.effective_message.reply_text(
                "Please send /start to request access to this bot."
            )
        return False

    if record.is_approved():
        return True

    if record.access_status.value == "suspended":
        if update.callback_query:
            await answer_callback(update.callback_query, "Access suspended.", show_alert=True)
        elif update.effective_message:
            await update.effective_message.reply_text(
                "🚫 Your access is suspended. Contact the bot owner."
            )
        return False

    # pending
    if update.callback_query:
        await answer_callback(
            update.callback_query, "Access pending owner approval.", show_alert=True
        )
    elif update.effective_message:
        await update.effective_message.reply_text(
            "⏳ Your access request is pending owner approval.\n"
            "Send /start to check status."
        )
    return False


def guarded(
    handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Any],
) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Any]:
    """Decorator: enforce access control before running a handler."""

    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        if not await check_access(update, context):
            return None
        return await handler(update, context)

    return wrapper


def owner_only(
    handler: Callable[[Update, ContextTypes.DEFAULT_TYPE], Any],
) -> Callable[[Update, ContextTypes.DEFAULT_TYPE], Any]:
    """Decorator: owner-only commands (access gate skipped)."""

    @wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        if not is_bot_owner(context, user_id(update)):
            if update.effective_message:
                await update.effective_message.reply_text("Owner only.")
            elif update.callback_query:
                await answer_callback(update.callback_query, "Owner only.", show_alert=True)
            return None
        return await handler(update, context)

    return wrapper
