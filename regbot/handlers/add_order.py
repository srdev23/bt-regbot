from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from regbot.formatting import (
    addorder_step,
    confirm_order_message,
    esc,
    fmt_tao,
    wallet_picker_message,
)
from regbot.handlers.common import chain, monitor, notifier, settings, store, wallet_path, wallets
from regbot.models import RegistrationOrder
from regbot.wallet_discovery import discover_wallets

logger = logging.getLogger(__name__)

NETUID = "netuid"
WALLET = "wallet"
HOTKEY = "hotkey"
REG_AT = "reg_at"
DURATION = "duration"
POLL_INTERVAL = "poll_interval"
PASSWORD = "password"
CONFIRM = "confirm"

TOTAL_STEPS = 6

DURATION_PATTERN = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$",
    re.IGNORECASE,
)

SendFn = Callable[..., Awaitable[Any]]


def _state(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    return context.user_data.get("addorder_state")


def _set_state(context: ContextTypes.DEFAULT_TYPE, state: str | None) -> None:
    if state is None:
        context.user_data.pop("addorder_state", None)
    else:
        context.user_data["addorder_state"] = state


def _clear_order(context: ContextTypes.DEFAULT_TYPE) -> None:
    _set_state(context, None)
    context.user_data.pop("new_order", None)
    context.user_data.pop("wallet_choices", None)
    context.user_data.pop("wallet_balances", None)
    context.user_data.pop("available_hotkeys", None)


def _wallet_choices(context: ContextTypes.DEFAULT_TYPE) -> list[dict]:
    return context.user_data.get("wallet_choices", [])


def _parse_duration(text: str) -> int | None:
    text = text.strip().lower()
    match = DURATION_PATTERN.match(text)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)[0]
    if unit == "s":
        return int(value)
    if unit == "m":
        return int(value * 60)
    if unit == "h":
        return int(value * 3600)
    if unit == "d":
        return int(value * 86400)
    return None


async def addorder_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["new_order"] = {}
    _set_state(context, NETUID)
    await notifier(context).reply_html(
        update.effective_message,
        addorder_step(1, TOTAL_STEPS, "Subnet", "Enter the <b>netuid</b>, e.g. <code>62</code>"),
    )


async def addorder_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _state(context) is None:
        return
    _clear_order(context)
    await notifier(context).reply_html(update.message, "⛔ Order creation cancelled.")


async def addorder_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = _state(context)
    if state is None:
        return

    if state == NETUID:
        await _handle_netuid(update, context)
    elif state == WALLET:
        await _handle_wallet_text(update, context)
    elif state == HOTKEY:
        await _handle_hotkey_text(update, context)
    elif state == REG_AT:
        await _handle_reg_at(update, context)
    elif state == DURATION:
        await _handle_duration(update, context)
    elif state == POLL_INTERVAL:
        await notifier(context).reply_html(
            update.message,
            "Tap an interval button: <b>3s</b>, <b>5s</b>, or <b>10s</b>",
        )
    elif state == PASSWORD:
        await _handle_password(update, context)


async def addorder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    data = (query.data or "").strip()
    state = _state(context)
    notify = notifier(context)

    logger.info("addorder callback data=%r state=%r user=%s", data, state, update.effective_user.id)

    try:
        await query.answer()
    except Exception:
        logger.exception("Failed to answer callback query")

    if state is None:
        if any(data.startswith(p) for p in ("wallet:", "hotkey:", "poll:", "confirm:")):
            await notify.edit_html(query, "Session expired. Run /addorder again.")
        return

    try:
        if data.startswith("wallet:"):
            await _apply_wallet_choice(context, idx=data.split(":", 1)[1], send=notify.edit_html, target=query)
        elif data.startswith("hotkey:"):
            await _apply_hotkey_choice(
                context, hotkey=data.split(":", 1)[1], send=notify.edit_html, target=query
            )
        elif data.startswith("poll:"):
            if state != POLL_INTERVAL:
                await notify.edit_html(query, "Wrong step. Run /addorder again.")
                return
            await _apply_poll_choice(
                context, int(data.split(":", 1)[1]), send=notify.edit_html, target=query
            )
        elif data.startswith("confirm:"):
            if state != CONFIRM:
                await notify.edit_html(query, "Wrong step. Run /addorder again.")
                return
            await _apply_confirm(
                update, context, data.endswith(":yes"), send=notify.edit_html, target=query
            )
        else:
            await notify.edit_html(query, "Unknown action. Run /addorder again.")
    except Exception:
        logger.exception("addorder callback failed data=%r state=%r", data, state)
        await notify.edit_html(query, "Something went wrong. Run /addorder again.")
        _clear_order(context)


async def _handle_netuid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    notify = notifier(context)
    text = update.message.text.strip()
    if not text.isdigit():
        await notify.reply_html(update.message, "Send a valid netuid number.")
        return

    netuid = int(text)
    svc = chain(context)
    await notify.reply_html(update.message, f"🔍 Checking subnet <b>SN{netuid}</b>…")
    if not await svc.subnet_exists(netuid):
        await notify.reply_html(update.message, f"Subnet <b>SN{netuid}</b> does not exist.")
        return

    context.user_data["new_order"]["netuid"] = netuid
    path = settings(context).wallet_path
    wallets = discover_wallets(path)
    if not wallets:
        await notify.reply_html(update.message, f"No wallets in <code>{esc(path)}</code>")
        _clear_order(context)
        return

    context.user_data["wallet_choices"] = [{"name": w.name, "hotkeys": list(w.hotkeys)} for w in wallets]
    balances = await svc.fetch_wallet_balances(str(path), wallets)
    context.user_data["wallet_balances"] = balances

    text, markup = _wallet_picker_content(context)
    await notify.reply_html(update.message, text, reply_markup=markup)
    _set_state(context, WALLET)


async def _handle_wallet_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = update.message.text.strip()
    wallets = _wallet_choices(context)
    match = next((w for w in wallets if w["name"].lower() == name.lower()), None)
    if match is None:
        names = ", ".join(f"<code>{esc(w['name'])}</code>" for w in wallets)
        await notifier(context).reply_html(
            update.message, f"Unknown wallet. Choose: {names}"
        )
        return
    await _apply_wallet_choice(
        context,
        idx=str(wallets.index(match)),
        send=notifier(context).reply_html,
        target=update.message,
    )


def _wallet_picker_content(context: ContextTypes.DEFAULT_TYPE) -> tuple[str, InlineKeyboardMarkup]:
    balances = context.user_data.get("wallet_balances", {})
    choices = context.user_data["wallet_choices"]
    body = wallet_picker_message(balances, choices)
    buttons = [
        [InlineKeyboardButton(w["name"], callback_data=f"wallet:{idx}")]
        for idx, w in enumerate(choices)
    ]
    text = addorder_step(2, TOTAL_STEPS, "Wallet", body)
    return text, InlineKeyboardMarkup(buttons)


async def _apply_wallet_choice(context, *, idx: str, send: SendFn, target) -> None:
    if _state(context) != WALLET:
        await send(target, "Wrong step. Run /addorder again.")
        return

    wallets = _wallet_choices(context)
    try:
        wallet = wallets[int(idx)]
    except (IndexError, ValueError):
        await send(target, "Wallet selection expired. Run /addorder again.")
        _clear_order(context)
        return

    context.user_data["new_order"]["wallet_name"] = wallet["name"]
    balances = context.user_data.get("wallet_balances", {})
    balance = balances.get(wallet["name"])
    bal_line = f"\nBalance {fmt_tao(balance)}" if balance is not None else ""

    netuid = context.user_data["new_order"]["netuid"]
    available = await chain(context).filter_unregistered_hotkeys(
        netuid, wallet["name"], wallet_path(context), wallet["hotkeys"]
    )
    context.user_data["available_hotkeys"] = available

    if not available:
        registered = ", ".join(f"<code>{esc(hk)}</code>" for hk in wallet["hotkeys"])
        picker_text, picker_markup = _wallet_picker_content(context)
        await send(
            target,
            f"⚠️ All hotkeys on <code>{esc(wallet['name'])}</code> are already "
            f"registered on <b>SN{netuid}</b>:\n{registered}\n\n{picker_text}",
            reply_markup=picker_markup,
        )
        context.user_data["new_order"].pop("wallet_name", None)
        context.user_data.pop("available_hotkeys", None)
        _set_state(context, WALLET)
        return

    buttons = [[InlineKeyboardButton(hk, callback_data=f"hotkey:{hk}")] for hk in available]
    hotkeys = ", ".join(f"<code>{esc(hk)}</code>" for hk in available)
    skipped = len(wallet["hotkeys"]) - len(available)
    skip_note = f"\n<i>{skipped} already registered on SN{netuid} — hidden</i>" if skipped else ""
    await send(
        target,
        addorder_step(
            3, TOTAL_STEPS, "Hotkey",
            f"Wallet <code>{esc(wallet['name'])}</code> selected{bal_line}\n"
            f"Available hotkeys on <b>SN{netuid}</b>:\n{hotkeys}{skip_note}",
        ),
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    _set_state(context, HOTKEY)


async def _handle_hotkey_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    hotkey = update.message.text.strip()
    available = context.user_data.get("available_hotkeys", [])
    if hotkey not in available:
        valid = ", ".join(f"<code>{esc(hk)}</code>" for hk in available)
        await notifier(context).reply_html(
            update.message,
            f"Hotkey not available. Choose an unregistered hotkey:\n{valid or 'none — pick another wallet'}",
        )
        return
    await _apply_hotkey_choice(
        context, hotkey=hotkey, send=notifier(context).reply_html, target=update.message
    )


async def _apply_hotkey_choice(context, *, hotkey: str, send: SendFn, target) -> None:
    if _state(context) != HOTKEY:
        await send(target, "Wrong step. Run /addorder again.")
        return

    available = context.user_data.get("available_hotkeys", [])
    if hotkey not in available:
        await send(target, "That hotkey is already registered on this subnet. Pick another.")
        return

    context.user_data["new_order"]["hotkey"] = hotkey
    await send(
        target,
        addorder_step(
            4, TOTAL_STEPS, "Target price",
            f"Hotkey <code>{esc(hotkey)}</code> selected.\n"
            f"Enter max registration price in TAO (<b>regAt</b>), e.g. <code>0.5</code>",
        ),
    )
    _set_state(context, REG_AT)


async def _handle_reg_at(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    notify = notifier(context)
    try:
        reg_at = float(update.message.text.strip())
        if reg_at <= 0:
            raise ValueError
    except ValueError:
        await notify.reply_html(update.message, "Send a positive number for regAt (TAO).")
        return

    context.user_data["new_order"]["reg_at"] = reg_at
    await notify.reply_html(
        update.message,
        addorder_step(
            5, TOTAL_STEPS, "Duration",
            "Monitoring window? e.g. <code>30m</code>, <code>6h</code>, <code>24h</code>, <code>7d</code>",
        ),
    )
    _set_state(context, DURATION)


async def _handle_duration(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    notify = notifier(context)
    duration_sec = _parse_duration(update.message.text)
    if duration_sec is None or duration_sec < 60:
        await notify.reply_html(update.message, "Invalid duration. Use <code>30m</code>, <code>6h</code>, etc.")
        return

    context.user_data["new_order"]["duration_sec"] = duration_sec
    buttons = [
        [
            InlineKeyboardButton("3s", callback_data="poll:3"),
            InlineKeyboardButton("5s", callback_data="poll:5"),
            InlineKeyboardButton("10s", callback_data="poll:10"),
        ]
    ]
    await notify.reply_html(
        update.message,
        addorder_step(6, TOTAL_STEPS, "Poll interval", "How often to check the reg price?"),
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    _set_state(context, POLL_INTERVAL)


async def _apply_poll_choice(context, poll: int, *, send: SendFn, target) -> None:
    context.user_data["new_order"]["poll_interval_sec"] = poll
    wallet_name = context.user_data["new_order"]["wallet_name"]
    sessions = wallets(context)
    path = wallet_path(context)
    if sessions.has_password(wallet_name) or sessions.try_unlock_passwordless(
        wallet_name, context.user_data["new_order"]["hotkey"], path
    ):
        await _show_confirm(send, target, context)
        return

    await send(
        target,
        f"🔐 Enter coldkey password for <code>{esc(wallet_name)}</code>.\n"
        "<i>Kept in memory only — never saved.</i>",
    )
    _set_state(context, PASSWORD)


async def _handle_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    notify = notifier(context)
    password = update.message.text
    try:
        await update.message.delete()
    except Exception:
        pass

    wallet_name = context.user_data["new_order"]["wallet_name"]
    hotkey = context.user_data["new_order"]["hotkey"]
    path = wallet_path(context)
    sessions = wallets(context)
    sessions.set_password(wallet_name, password)
    try:
        sessions.unlock(wallet_name, hotkey, path)
    except ValueError as exc:
        sessions.forget(wallet_name)
        await notify.reply_html(update.message, f"{esc(exc)}\nSend password again:")
        return

    await notify.reply_html(update.message, "✅ Password accepted.")
    await _show_confirm(notify.reply_html, update.message, context)


async def _show_confirm(send: SendFn, target, context: ContextTypes.DEFAULT_TYPE) -> None:
    draft = context.user_data["new_order"]
    cfg = settings(context)
    balance = await chain(context).get_coldkey_balance_tao(
        draft["wallet_name"], wallet_path(context), draft["hotkey"]
    )
    buttons = [
        [
            InlineKeyboardButton("✅ Confirm", callback_data="confirm:yes"),
            InlineKeyboardButton("❌ Cancel", callback_data="confirm:no"),
        ]
    ]
    await send(
        target,
        confirm_order_message(draft, balance, slippage_pct=cfg.slippage_tolerance * 100),
        reply_markup=InlineKeyboardMarkup(buttons),
    )
    _set_state(context, CONFIRM)


async def _apply_confirm(
    update: Update, context: ContextTypes.DEFAULT_TYPE, confirmed: bool, *, send: SendFn, target
) -> None:
    if not confirmed:
        await send(target, "⛔ Order creation cancelled.")
        _clear_order(context)
        return

    draft = context.user_data.pop("new_order", None)
    _set_state(context, None)
    context.user_data.pop("wallet_choices", None)

    if not draft:
        await send(target, "Session expired. Run /addorder again.")
        return

    order = RegistrationOrder.create(
        user_id=update.effective_user.id,
        chat_id=update.effective_chat.id,
        netuid=draft["netuid"],
        wallet_name=draft["wallet_name"],
        hotkey=draft["hotkey"],
        reg_at=draft["reg_at"],
        duration_sec=draft["duration_sec"],
        poll_interval_sec=draft["poll_interval_sec"],
    )

    store(context).save(order)
    await monitor(context).track(order.id)

    await send(
        target,
        f"✅ <b>Order created</b> · <code>{esc(order.id[:8])}</code>\n"
        f"Monitoring <b>SN{order.netuid}</b> every {order.poll_interval_sec}s\n"
        f"Auto-register when price ≤ {order.reg_at} τ",
    )
