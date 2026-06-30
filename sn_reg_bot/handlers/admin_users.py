from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from sn_reg_bot.formatting_owner import fmt_user_detail, fmt_users_page
from sn_reg_bot.handlers.common import (
    access_service,
    admin_service,
    is_bot_owner,
    settings,
    user_id,
)
from sn_reg_bot.handlers.progress import run_with_progress
from sn_reg_bot.handlers.tg_helpers import answer_callback
from sn_reg_bot.models.access import UserAccessStatus


async def _require_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if is_bot_owner(context, user_id(update)):
        return True
    if update.effective_message:
        await update.effective_message.reply_text("Owner only.")
    return False


async def applications_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update, context):
        return
    pending = await access_service(context).user_repository.list_by_access_status(
        UserAccessStatus.PENDING, limit=50
    )
    if not pending:
        await update.effective_message.reply_text("No pending access requests.")
        return

    lines = ["<b>Pending access requests</b>\n"]
    keyboard_rows = []
    for record in pending:
        lines.append(
            f"• {record.full_label()} · <code>{record.telegram_user_id}</code>"
        )
        keyboard_rows.append(
            [
                InlineKeyboardButton(
                    f"✅ {record.display_name()[:20]}",
                    callback_data=f"useradmin:approve:{record.telegram_user_id}",
                ),
                InlineKeyboardButton(
                    "🚫 Deny",
                    callback_data=f"useradmin:suspend:{record.telegram_user_id}",
                ),
            ]
        )
    await update.effective_message.reply_text(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(keyboard_rows),
        parse_mode="HTML",
    )


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update, context):
        return
    page = 1
    if context.args:
        try:
            page = max(1, int(context.args[0]))
        except ValueError:
            await update.effective_message.reply_text("Usage: /users [page]")
            return

    cfg = settings(context)
    per_page = cfg.owner_users_per_page

    async def _build():
        return await admin_service(context).build_usage_report(
            orders_per_user=5,
            include_order_details=False,
        )

    report = await run_with_progress(
        update.effective_message,
        _build,
        text="⏳ Loading users...",
    )
    text, total_pages = fmt_users_page(report, page=page, per_page=per_page)
    nav = []
    if page > 1:
        nav.append(
            InlineKeyboardButton("◀ Prev", callback_data=f"useradmin:page:{page - 1}")
        )
    if page < total_pages:
        nav.append(
            InlineKeyboardButton("Next ▶", callback_data=f"useradmin:page:{page + 1}")
        )
    markup = InlineKeyboardMarkup([nav]) if nav else None
    await update.effective_message.reply_text(text, reply_markup=markup, parse_mode="HTML")


async def user_detail_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /user <telegram_user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("User id must be a number.")
        return

    summary = await admin_service(context).get_user_summary(target_id)
    if summary is None:
        await update.effective_message.reply_text("User not found.")
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ Approve", callback_data=f"useradmin:approve:{target_id}"
                ),
                InlineKeyboardButton(
                    "🚫 Suspend", callback_data=f"useradmin:suspend:{target_id}"
                ),
            ]
        ]
    )
    await update.effective_message.reply_text(
        fmt_user_detail(summary),
        reply_markup=keyboard,
        parse_mode="HTML",
    )


async def approve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /approve <telegram_user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("User id must be a number.")
        return
    record = await access_service(context).approve_user(target_id)
    if record is None:
        await update.effective_message.reply_text("Could not approve user.")
        return
    await update.effective_message.reply_text(
        f"✅ Approved {record.full_label()} (<code>{target_id}</code>).",
        parse_mode="HTML",
    )


async def suspend_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_owner(update, context):
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /suspend <telegram_user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("User id must be a number.")
        return
    if target_id == user_id(update):
        await update.effective_message.reply_text("You cannot suspend yourself.")
        return
    record = await access_service(context).suspend_user(target_id)
    if record is None:
        await update.effective_message.reply_text("Could not suspend user.")
        return
    await update.effective_message.reply_text(
        f"🚫 Suspended {record.full_label()} (<code>{target_id}</code>).",
        parse_mode="HTML",
    )


async def useradmin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_bot_owner(context, user_id(update)):
        await answer_callback(update.callback_query, "Owner only.")
        return

    query = update.callback_query
    await answer_callback(query)
    data = query.data or ""

    if data.startswith("useradmin:page:"):
        page = int(data.split(":")[-1])
        cfg = settings(context)
        report = await admin_service(context).build_usage_report(
            orders_per_user=5,
            include_order_details=False,
        )
        text, total_pages = fmt_users_page(
            report, page=page, per_page=cfg.owner_users_per_page
        )
        nav = []
        if page > 1:
            nav.append(
                InlineKeyboardButton("◀ Prev", callback_data=f"useradmin:page:{page - 1}")
            )
        if page < total_pages:
            nav.append(
                InlineKeyboardButton("Next ▶", callback_data=f"useradmin:page:{page + 1}")
            )
        markup = InlineKeyboardMarkup([nav]) if nav else None
        await query.edit_message_text(text, reply_markup=markup, parse_mode="HTML")
        return

    if data.startswith("useradmin:approve:"):
        target_id = int(data.split(":")[-1])
        record = await access_service(context).approve_user(target_id)
        if record is None:
            await query.edit_message_text("Could not approve user.")
            return
        await query.edit_message_text(
            f"✅ Approved {record.full_label()} (<code>{target_id}</code>).",
            parse_mode="HTML",
        )
        return

    if data.startswith("useradmin:suspend:"):
        target_id = int(data.split(":")[-1])
        if target_id == user_id(update):
            await answer_callback(query, "Cannot suspend yourself.")
            return
        record = await access_service(context).suspend_user(target_id)
        if record is None:
            await query.edit_message_text("Could not suspend user.")
            return
        await query.edit_message_text(
            f"🚫 Suspended {record.full_label()} (<code>{target_id}</code>).",
            parse_mode="HTML",
        )
