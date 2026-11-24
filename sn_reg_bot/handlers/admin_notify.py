from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import ContextTypes

from sn_reg_bot.handlers.common import is_bot_owner, notifier, user_id, users_repo
from sn_reg_bot.models.access import UserAccessStatus

logger = logging.getLogger(__name__)


async def notify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Owner-only: send a Telegram message to one user or all approved users."""
    if not is_bot_owner(context, user_id(update)):
        await update.effective_message.reply_text("Owner only.")
        return

    args = context.args or []
    if len(args) < 2:
        await update.effective_message.reply_text(
            "<b>Usage</b>\n"
            "/notify all &lt;message&gt;\n"
            "/notify &lt;telegram_user_id&gt; &lt;message&gt;",
            parse_mode="HTML",
        )
        return

    target = args[0]
    message = " ".join(args[1:]).strip()
    if not message:
        await update.effective_message.reply_text("Message cannot be empty.")
        return

    notify = notifier(context)
    msg = update.effective_message

    if target.lower() == "all":
        users = await users_repo(context).list_all()
        recipients = [
            u for u in users if u.access_status == UserAccessStatus.APPROVED
        ]
        if not recipients:
            await msg.reply_text("No approved users to notify.")
            return
        sent = 0
        failed = 0
        for record in recipients:
            try:
                await notify.send(record.telegram_user_id, message)
                sent += 1
            except Exception as exc:
                failed += 1
                logger.warning(
                    "Notify failed user=%s: %s", record.telegram_user_id, exc
                )
        await msg.reply_text(
            f"📣 Broadcast finished: {sent} sent, {failed} failed "
            f"(of {len(recipients)} approved users)."
        )
        return

    try:
        target_id = int(target)
    except ValueError:
        await msg.reply_text("Target must be <code>all</code> or a numeric user id.")
        return

    record = await users_repo(context).get(target_id)
    if record is None:
        await msg.reply_text("User not found.")
        return

    try:
        await notify.send(target_id, message)
    except Exception as exc:
        await msg.reply_text(f"Failed to send: {exc}")
        return

    await msg.reply_text(
        f"✅ Sent notification to {record.full_label()} (<code>{target_id}</code>).",
        parse_mode="HTML",
    )
