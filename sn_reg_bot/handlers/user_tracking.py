from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from sn_reg_bot.handlers.common import settings


async def track_user_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Persist Telegram profile fields whenever a user interacts with the bot."""
    user = update.effective_user
    if user is None:
        return
    users_repo = context.application.bot_data.get("users_repo")
    if users_repo is None:
        return
    try:
        cfg = settings(context)
        await users_repo.upsert_profile(user, owner_id=cfg.bot_owner_id)
    except Exception:
        import logging

        logging.getLogger(__name__).exception(
            "Failed to sync user profile for %s", user.id
        )
