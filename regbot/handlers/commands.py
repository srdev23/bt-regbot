from __future__ import annotations

import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from regbot.formatting import (
    esc,
    fmt_tao,
    fmt_wallet_line,
    header,
    order_card,
    orders_list_title,
    status_summary,
    welcome_message,
)
from regbot.handlers.add_order import PASSWORD
from regbot.models import OrderStatus
from regbot.notifier import TelegramNotifier
from regbot.order_store import INACTIVE_STATUSES
from regbot.wallet_discovery import discover_wallets


def _notifier(context: ContextTypes.DEFAULT_TYPE) -> TelegramNotifier:
    return context.application.bot_data["notifier"]


def _settings(context: ContextTypes.DEFAULT_TYPE):
    return context.application.bot_data["settings"]


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _notifier(context).reply_html(update.message, welcome_message())


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_cmd(update, context)


async def _fetch_order_balances(context, orders) -> dict[str, float | None]:
    chain = context.application.bot_data["chain"]
    wallet_path = str(context.application.bot_data["settings"].wallet_path)
    unique: dict[str, str] = {}
    for order in orders:
        unique.setdefault(order.wallet_name, order.hotkey)

    async def _one(name: str, hotkey: str) -> tuple[str, float | None]:
        bal = await chain.get_coldkey_balance_tao(name, wallet_path, hotkey)
        return name, bal

    pairs = await asyncio.gather(*(_one(n, hk) for n, hk in unique.items()))
    return dict(pairs)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store = context.application.bot_data["store"]
    active = store.list_for_user(update.effective_user.id, statuses={OrderStatus.ACTIVE})
    active = [o for o in active if not o.is_expired()]
    if not active:
        await _notifier(context).reply_html(update.message, status_summary(active))
        return
    balances = await _fetch_order_balances(context, active)
    lines = [header("Monitoring Status", f"{len(active)} active order(s)"), ""]
    for order in active:
        lines.append(order_card(order, balance=balances.get(order.wallet_name)))
        lines.append("")
    await _notifier(context).reply_html(update.message, "\n".join(lines).strip())


async def wallets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    wallet_path = context.application.bot_data["settings"].wallet_path
    wallets = discover_wallets(wallet_path)
    if not wallets:
        await _notifier(context).reply_html(
            update.message, f"No wallets in <code>{esc(wallet_path)}</code>"
        )
        return

    chain = context.application.bot_data["chain"]
    balances = await chain.fetch_wallet_balances(str(wallet_path), wallets)
    lines = [header("Local Wallets", str(wallet_path)), ""]
    for w in wallets:
        lines.append(fmt_wallet_line(w.name, balances.get(w.name), w.hotkeys))
    await _notifier(context).reply_html(update.message, "\n".join(lines))


async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await _notifier(context).reply_html(update.message, "Usage: /price &lt;netuid&gt;")
        return

    netuid = int(context.args[0])
    chain = context.application.bot_data["chain"]
    if not await chain.subnet_exists(netuid):
        await _notifier(context).reply_html(update.message, f"Subnet <b>SN{netuid}</b> not found.")
        return

    price = await chain.get_registration_price(netuid)
    await _notifier(context).reply_html(
        update.message,
        f"💰 <b>SN{netuid}</b> registration price\n{fmt_tao(price.burn_tao)}",
    )


async def orders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store = context.application.bot_data["store"]
    settings = _settings(context)
    user_id = update.effective_user.id
    mode = (context.args[0].lower() if context.args else "active")

    if mode == "history":
        all_inactive = store.list_for_user(user_id, statuses=set(INACTIVE_STATUSES))
        total = len(all_inactive)
        orders = all_inactive[: settings.order_history_limit]
        title_mode = "history"
    elif mode == "all":
        active = [o for o in store.list_for_user(user_id, statuses={OrderStatus.ACTIVE}) if not o.is_expired()]
        history = store.list_for_user(user_id, statuses=set(INACTIVE_STATUSES))[: settings.order_history_limit]
        orders = active + history
        total = len(orders)
        title_mode = "all"
    else:
        active_all = [o for o in store.list_for_user(user_id, statuses={OrderStatus.ACTIVE}) if not o.is_expired()]
        total = len(active_all)
        orders = active_all[: settings.order_list_limit]
        title_mode = "active"

    if not orders:
        hints = {
            "active": "No active orders. Use /addorder to create one.",
            "history": "No finished orders in history.",
            "all": "No orders found.",
        }
        await _notifier(context).reply_html(
            update.message,
            f"{header('Orders')}\n{hints.get(title_mode, 'No orders.')}",
        )
        return

    lines = [orders_list_title(title_mode, total, len(orders)), ""]
    balances = await _fetch_order_balances(context, orders) if title_mode != "history" else {}
    for order in orders:
        bal = balances.get(order.wallet_name) if title_mode != "history" else None
        lines.append(order_card(order, balance=bal))
        lines.append("")
    if title_mode == "active":
        lines.append("<i>/orders history · /order &lt;id&gt; · /clearorders</i>")
    elif title_mode == "history" and total > len(orders):
        lines.append(f"<i>Showing last {len(orders)} of {total}. Use /clearorders to tidy up.</i>")
    await _notifier(context).reply_html(update.message, "\n".join(lines).strip())


async def order_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await _notifier(context).reply_html(update.message, "Usage: /order &lt;id_prefix&gt;")
        return

    store = context.application.bot_data["store"]
    matches = store.find_for_user(update.effective_user.id, context.args[0])
    if not matches:
        await _notifier(context).reply_html(update.message, "No order matches that id.")
        return
    if len(matches) > 1:
        await _notifier(context).reply_html(
            update.message, "Multiple matches — use a longer id prefix."
        )
        return

    order = matches[0]
    balances = await _fetch_order_balances(context, [order])
    await _notifier(context).reply_html(
        update.message,
        f"{header('Order Detail')}\n{order_card(order, detailed=True, balance=balances.get(order.wallet_name))}",
    )


async def clearorders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    store = context.application.bot_data["store"]
    settings = _settings(context)
    user_id = update.effective_user.id

    if context.args and context.args[0].lower() == "all":
        removed = store.purge_inactive(user_id=user_id, all_inactive=True)
        label = "all finished"
    else:
        removed = store.purge_inactive(user_id=user_id, older_than_days=settings.order_history_days)
        label = f"older than {settings.order_history_days} days"

    if removed == 0:
        await _notifier(context).reply_html(
            update.message,
            f"🧹 Nothing to clear ({label}).\n"
            f"<i>Active orders are never deleted.</i>",
        )
        return

    await _notifier(context).reply_html(
        update.message,
        f"🧹 Cleared <b>{removed}</b> finished order(s) ({label}).",
    )


async def cancelorder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await _notifier(context).reply_html(update.message, "Usage: /cancelorder &lt;id_prefix&gt;")
        return

    prefix = context.args[0].lower()
    store = context.application.bot_data["store"]
    monitor = context.application.bot_data["monitor"]
    user_id = update.effective_user.id

    matches = [
        o
        for o in store.find_for_user(user_id, prefix)
        if o.status == OrderStatus.ACTIVE and not o.is_expired()
    ]
    if not matches:
        await _notifier(context).reply_html(update.message, "No active order matches that id.")
        return
    if len(matches) > 1:
        await _notifier(context).reply_html(update.message, "Multiple matches — use a longer id.")
        return

    order = matches[0]
    order.status = OrderStatus.CANCELLED
    store.save(order)
    await monitor.untrack(order.id)
    await _notifier(context).reply_html(
        update.message,
        f"⛔ Cancelled order <code>{esc(order.id[:8])}</code>",
    )


async def unlock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 1:
        await _notifier(context).reply_html(
            update.message,
            "Usage: /unlock &lt;wallet_name&gt;\nThen send the coldkey password.",
        )
        return

    wallet_name = context.args[0]
    wallet_path = context.application.bot_data["settings"].wallet_path
    wallets = discover_wallets(wallet_path)
    if not any(w.name == wallet_name for w in wallets):
        await _notifier(context).reply_html(
            update.message, f"Wallet <code>{esc(wallet_name)}</code> not found."
        )
        return

    context.user_data["unlock_wallet"] = wallet_name
    await _notifier(context).reply_html(
        update.message,
        f"🔐 Send coldkey password for <code>{esc(wallet_name)}</code>.\n"
        "<i>Message will be deleted after reading.</i>",
    )


async def unlock_password_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("addorder_state") == PASSWORD:
        return
    wallet_name = context.user_data.pop("unlock_wallet", None)
    if not wallet_name:
        return

    password = update.message.text
    try:
        await update.message.delete()
    except Exception:
        pass

    wallet_sessions = context.application.bot_data["wallets"]
    wallet_path = str(context.application.bot_data["settings"].wallet_path)
    wallet_info = next(
        w
        for w in discover_wallets(context.application.bot_data["settings"].wallet_path)
        if w.name == wallet_name
    )
    wallet_sessions.set_password(wallet_name, password)
    try:
        wallet_sessions.unlock(wallet_name, wallet_info.hotkeys[0], wallet_path)
    except ValueError as exc:
        wallet_sessions.forget(wallet_name)
        await _notifier(context).reply_html(update.message, f"Unlock failed: {esc(exc)}")
        return

    await _notifier(context).reply_html(
        update.message,
        f"✅ Wallet <code>{esc(wallet_name)}</code> unlocked for this session.",
    )
