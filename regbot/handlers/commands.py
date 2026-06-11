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
from regbot.handlers.common import chain, monitor, notifier, settings, store, wallet_path, wallets
from regbot.models import OrderStatus
from regbot.order_store import INACTIVE_STATUSES
from regbot.wallet_discovery import discover_wallets


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await notifier(context).reply_html(update.message, welcome_message())


help_cmd = start_cmd


async def _fetch_order_balances(context: ContextTypes.DEFAULT_TYPE, orders) -> dict[str, float | None]:
    unique = {order.wallet_name: order.hotkey for order in orders}

    async def _one(name: str, hotkey: str) -> tuple[str, float | None]:
        bal = await chain(context).get_coldkey_balance_tao(name, wallet_path(context), hotkey)
        return name, bal

    pairs = await asyncio.gather(*(_one(n, hk) for n, hk in unique.items()))
    return dict(pairs)


async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    active = store(context).list_active_for_user(update.effective_user.id)
    balances = await _fetch_order_balances(context, active) if active else None
    await notifier(context).reply_html(
        update.message, status_summary(active, balances)
    )


async def wallets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    path = settings(context).wallet_path
    wallets = discover_wallets(path)
    if not wallets:
        await notifier(context).reply_html(
            update.message, f"No wallets in <code>{esc(path)}</code>"
        )
        return

    balances = await chain(context).fetch_wallet_balances(str(path), wallets)
    lines = [header("Local Wallets", str(path)), ""]
    for w in wallets:
        lines.append(fmt_wallet_line(w.name, balances.get(w.name), w.hotkeys))
    await notifier(context).reply_html(update.message, "\n".join(lines))


async def price_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await notifier(context).reply_html(update.message, "Usage: /price &lt;netuid&gt;")
        return

    netuid = int(context.args[0])
    svc = chain(context)
    if not await svc.subnet_exists(netuid):
        await notifier(context).reply_html(update.message, f"Subnet <b>SN{netuid}</b> not found.")
        return

    price = await svc.get_registration_price(netuid)
    await notifier(context).reply_html(
        update.message,
        f"💰 <b>SN{netuid}</b> registration price\n{fmt_tao(price.burn_tao)}",
    )


async def orders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = store(context)
    cfg = settings(context)
    user_id = update.effective_user.id
    mode = (context.args[0].lower() if context.args else "active")

    if mode == "history":
        all_inactive = svc.list_for_user(user_id, statuses=set(INACTIVE_STATUSES))
        total = len(all_inactive)
        orders = all_inactive[: cfg.order_history_limit]
        title_mode = "history"
    elif mode == "all":
        active = svc.list_active_for_user(user_id)
        history = svc.list_for_user(user_id, statuses=set(INACTIVE_STATUSES))[: cfg.order_history_limit]
        orders = active + history
        total = len(orders)
        title_mode = "all"
    else:
        active_all = svc.list_active_for_user(user_id)
        total = len(active_all)
        orders = active_all[: cfg.order_list_limit]
        title_mode = "active"

    if not orders:
        hints = {
            "active": "No active orders. Use /addorder to create one.",
            "history": "No finished orders in history.",
            "all": "No orders found.",
        }
        await notifier(context).reply_html(
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
    await notifier(context).reply_html(update.message, "\n".join(lines).strip())


async def order_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await notifier(context).reply_html(update.message, "Usage: /order &lt;id_prefix&gt;")
        return

    matches = store(context).find_for_user(update.effective_user.id, context.args[0])
    if not matches:
        await notifier(context).reply_html(update.message, "No order matches that id.")
        return
    if len(matches) > 1:
        await notifier(context).reply_html(
            update.message, "Multiple matches — use a longer id prefix."
        )
        return

    order = matches[0]
    balances = await _fetch_order_balances(context, [order])
    await notifier(context).reply_html(
        update.message,
        f"{header('Order Detail')}\n"
        f"{order_card(order, detailed=True, balance=balances.get(order.wallet_name))}",
    )


async def clearorders_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    svc = store(context)
    cfg = settings(context)
    user_id = update.effective_user.id

    if context.args and context.args[0].lower() == "all":
        removed = svc.purge_inactive(user_id=user_id, all_inactive=True)
        label = "all finished"
    else:
        removed = svc.purge_inactive(user_id=user_id, older_than_days=cfg.order_history_days)
        label = f"older than {cfg.order_history_days} days"

    if removed == 0:
        await notifier(context).reply_html(
            update.message,
            f"🧹 Nothing to clear ({label}).\n"
            f"<i>Active orders are never deleted.</i>",
        )
        return

    await notifier(context).reply_html(
        update.message,
        f"🧹 Cleared <b>{removed}</b> finished order(s) ({label}).",
    )


async def cancelorder_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await notifier(context).reply_html(update.message, "Usage: /cancelorder &lt;id_prefix&gt;")
        return

    prefix = context.args[0].lower()
    svc = store(context)
    user_id = update.effective_user.id

    matches = svc.find_active_for_user(user_id, prefix)
    if not matches:
        await notifier(context).reply_html(update.message, "No active order matches that id.")
        return
    if len(matches) > 1:
        await notifier(context).reply_html(update.message, "Multiple matches — use a longer id.")
        return

    order = matches[0]
    await monitor(context).untrack(order.id)
    order.status = OrderStatus.CANCELLED
    order.price_message_id = None
    svc.save(order)
    await notifier(context).reply_html(
        update.message,
        f"⛔ Cancelled order <code>{esc(order.id[:8])}</code>",
    )


async def unlock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if len(context.args) < 1:
        await notifier(context).reply_html(
            update.message,
            "Usage: /unlock &lt;wallet_name&gt;\nThen send the coldkey password.",
        )
        return

    wallet_name = context.args[0]
    path = settings(context).wallet_path
    wallets = discover_wallets(path)
    if not any(w.name == wallet_name for w in wallets):
        await notifier(context).reply_html(
            update.message, f"Wallet <code>{esc(wallet_name)}</code> not found."
        )
        return

    context.user_data["unlock_wallet"] = wallet_name
    await notifier(context).reply_html(
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

    path = wallet_path(context)
    wallet_info = next(
        w for w in discover_wallets(settings(context).wallet_path) if w.name == wallet_name
    )
    sessions = wallets(context)
    sessions.set_password(wallet_name, password)
    try:
        sessions.unlock_all_hotkeys(wallet_name, wallet_info.hotkeys, path)
    except ValueError as exc:
        sessions.forget(wallet_name)
        await notifier(context).reply_html(update.message, f"Unlock failed: {esc(exc)}")
        return

    await notifier(context).reply_html(
        update.message,
        f"✅ Wallet <code>{esc(wallet_name)}</code> unlocked for this session.\n"
        f"<i>{len(wallet_info.hotkeys)} hotkey(s) ready.</i>",
    )
