from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from sn_reg_bot.formatting import fmt_order_detail, fmt_order_summary, fmt_tao
from sn_reg_bot.formatting_owner import fmt_owner_status_summary
from sn_reg_bot.handlers.common import (
    access_service,
    admin_service,
    chain,
    chat_id,
    is_bot_owner,
    monitor,
    order_service,
    settings,
    user_id,
    wallet_service,
)
from sn_reg_bot.handlers.progress import run_with_progress


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = user_id(update)
    telegram_user = update.effective_user
    record, message = await access_service(context).handle_start(
        telegram_user, chat_id=chat_id(update)
    )
    await wallet_service(context).ensure_user(uid)
    await update.effective_message.reply_text(message, parse_mode="HTML")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "<b>Wallet commands</b>\n"
        "/wallets — list your wallets, balances, and addresses\n"
        "/wallet &lt;name&gt; — coldkey status (address, hotkeys, subnets)\n"
        "/address [name] — copy coldkey address (fund / tao.app)\n"
        "/createwallet — create a new coldkey\n"
        "/importwallet — import coldkey from mnemonic or wallet file\n"
        "/createhotkey — create a hotkey on a coldkey\n"
        "/importhotkey — import hotkey (mnemonic or file)\n"
        "/unlock — unlock a wallet for signing\n"
        "/lock — lock a wallet (forget saved password)\n"
        "/removewallet — remove a wallet from your list\n\n"
        "<b>Order commands</b>\n"
        "/addorder — create a subnet registration order\n"
        "/orders — list your active orders\n"
        "/order &lt;id&gt; — order details (prefix ok)\n"
        "/editorder — change max price on an active order\n"
        "/cancelorder — cancel an active order\n\n"
        "<b>Info</b>\n"
        "/price &lt;netuid&gt; — current registration price\n"
        "/status — bot status\n"
        "/cancel — cancel current wizard",
        parse_mode="HTML",
    )


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = user_id(update)
    if is_bot_owner(context, uid):
        await _owner_status_cmd(update, context)
        return

    active = monitor(context).active_count
    user_orders = await order_service(context).list_active_orders(uid)
    await update.effective_message.reply_text(
        f"🤖 <b>Bot status</b>\n"
        f"Network: <code>{settings(context).bittensor_network}</code>\n"
        f"Monitor tick: {settings(context).monitor_tick_sec}s · "
        f"Price alerts: every {settings(context).price_notify_interval_sec}s\n"
        f"Active orders (global): {active}\n"
        f"Your active orders: {len(user_orders)}",
        parse_mode="HTML",
    )


async def _owner_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    cfg = settings(context)

    async def _build_report():
        return await admin_service(context).build_usage_report(
            include_order_details=False,
            orders_per_user=0,
        )

    report = await run_with_progress(
        msg,
        _build_report,
        text="⏳ Gathering bot overview...",
    )
    await msg.reply_text(fmt_owner_status_summary(report, cfg), parse_mode="HTML")


async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /price <netuid>")
        return
    try:
        netuid = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("Netuid must be an integer.")
        return

    if not await chain(context).subnet_exists(netuid):
        await update.effective_message.reply_text(f"Subnet SN{netuid} not found.")
        return

    async def _fetch():
        return await chain(context).get_registration_price(netuid)

    price = await run_with_progress(
        update.effective_message,
        _fetch,
        text=f"⏳ Fetching SN{netuid} registration price...",
    )
    await update.effective_message.reply_text(
        f"💰 <b>SN{netuid}</b> registration price\n{fmt_tao(price.burn_tao)}",
        parse_mode="HTML",
    )


async def orders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = user_id(update)
    orders = await order_service(context).list_active_orders(uid)
    if not orders:
        await update.effective_message.reply_text(
            "No active orders. Use /addorder to create one."
        )
        return
    lines = [fmt_order_summary(o) for o in orders]
    await update.effective_message.reply_text(
        "<b>Your active orders</b>\n" + "\n".join(lines) + "\n\nEdit max price: /editorder",
        parse_mode="HTML",
    )


async def order_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /order <order_id_prefix>")
        return
    prefix = context.args[0]
    uid = user_id(update)
    orders = await order_service(context).list_orders(uid, limit=100)
    matches = [o for o in orders if o.id.startswith(prefix)]
    if not matches:
        await update.effective_message.reply_text("Order not found.")
        return
    if len(matches) > 1:
        await update.effective_message.reply_text("Multiple orders match; use a longer prefix.")
        return
    await update.effective_message.reply_text(
        fmt_order_detail(matches[0]),
        parse_mode="HTML",
    )
