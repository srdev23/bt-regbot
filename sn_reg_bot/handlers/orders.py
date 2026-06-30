from __future__ import annotations

import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from sn_reg_bot.handlers.common import chain, chat_id, order_service, settings, user_id, wallet_service
from sn_reg_bot.handlers.order_match import resolve_order_by_prefix
from sn_reg_bot.handlers.progress import progress_message, run_with_progress
from sn_reg_bot.handlers.tg_helpers import answer_callback
from sn_reg_bot.handlers.wizard_state import clear_other_wizards
from sn_reg_bot.services.order_service import DuplicateActiveOrderError


async def addorder_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_other_wizards(context, "addorder")
    uid = user_id(update)
    wallets = wallet_service(context).manager_for(uid).list_wallets()
    if not wallets:
        await update.effective_message.reply_text(
            "No wallets found. Create or import a coldkey first."
        )
        return
    context.user_data["addorder"] = {"step": "netuid"}
    await update.effective_message.reply_text("Enter subnet netuid (e.g. 15):")


async def addorder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await answer_callback(query)
    flow = context.user_data.get("addorder")
    if not flow:
        return

    data = query.data
    if data.startswith("addorder:wallet:"):
        flow["coldkey"] = data.split(":", 2)[2]
        flow["step"] = "hotkey"
        uid = user_id(update)
        netuid = int(flow["netuid"])
        coldkey = flow["coldkey"]

        await query.edit_message_text(
            f"Coldkey: <b>{coldkey}</b>\n⏳ Checking hotkeys on SN{netuid}...",
            parse_mode="HTML",
        )

        try:
            hotkeys = await wallet_service(context).filter_hotkeys_for_new_order(
                uid, coldkey, netuid
            )
        except Exception as exc:
            await query.edit_message_text(f"❌ Failed to check hotkeys: {exc}")
            context.user_data.pop("addorder", None)
            return

        if not hotkeys:
            await query.edit_message_text(
                f"Coldkey <b>{coldkey}</b> has no available hotkeys on SN{netuid}.\n"
                "Hotkeys may already be registered, or have an active registration order.",
                parse_mode="HTML",
            )
            context.user_data.pop("addorder", None)
            return

        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(hk, callback_data=f"addorder:hotkey:{hk}")] for hk in hotkeys]
        )
        await query.edit_message_text(
            f"Coldkey: <b>{coldkey}</b> · SN{netuid}\n"
            "Select hotkey:",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    if data.startswith("addorder:hotkey:"):
        flow["hotkey"] = data.split(":", 2)[2]
        flow["step"] = "price"
        await query.edit_message_text(
            f"Hotkey: <b>{flow['hotkey']}</b>\n"
            "Enter max registration price in TAO (register when price ≤ this value):",
            parse_mode="HTML",
        )
        return

    if data == "addorder:confirm:yes":
        uid = user_id(update)
        cid = chat_id(update)
        await query.edit_message_text("⏳ Creating order...")
        try:
            order = await order_service(context).create_subnet_registration_order(
                user_id=uid,
                chat_id=cid,
                netuid=int(flow["netuid"]),
                coldkey_name=flow["coldkey"],
                hotkey_name=flow["hotkey"],
                max_price_tao=float(flow["max_price"]),
            )
            context.user_data.pop("addorder", None)
            await query.edit_message_text(
                f"✅ Order created: <code>{order.id[:8]}</code>\n"
                f"Monitoring SN{order.params.netuid} for ≤ {order.params.max_price_tao:.6f} TAO.\n"
                f"Price alerts every ~{settings(context).price_notify_interval_sec}s.",
                parse_mode="HTML",
            )
        except DuplicateActiveOrderError as exc:
            await query.edit_message_text(f"❌ {exc}")
            context.user_data.pop("addorder", None)
        except Exception as exc:
            await query.edit_message_text(f"❌ Failed: {exc}")
            context.user_data.pop("addorder", None)
        return

    if data == "addorder:confirm:no":
        context.user_data.pop("addorder", None)
        await query.edit_message_text("Cancelled.")


async def addorder_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow = context.user_data.get("addorder")
    if not flow:
        return
    text = (update.effective_message.text or "").strip()
    uid = user_id(update)
    msg = update.effective_message

    if flow["step"] == "netuid":
        try:
            netuid = int(text)
            if netuid < 0:
                raise ValueError
        except ValueError:
            await msg.reply_text("Enter a valid non-negative netuid.")
            return

        async def _validate_subnet() -> bool:
            return await chain(context).subnet_exists(netuid)

        if not await run_with_progress(
            msg, _validate_subnet, text=f"⏳ Validating SN{netuid}..."
        ):
            await msg.reply_text(f"Subnet SN{netuid} not found.")
            return

        flow["netuid"] = netuid
        flow["step"] = "wallet"
        wallets = wallet_service(context).manager_for(uid).list_wallets()
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(w.name, callback_data=f"addorder:wallet:{w.name}")] for w in wallets]
        )
        await msg.reply_text(
            f"Subnet: SN{netuid}\nSelect coldkey:",
            reply_markup=keyboard,
        )
        return

    if flow["step"] == "price":
        try:
            price = float(text)
            if price <= 0:
                raise ValueError
        except ValueError:
            await msg.reply_text("Enter a positive number for max price in TAO.")
            return
        flow["max_price"] = price
        flow["step"] = "confirm"
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Confirm", callback_data="addorder:confirm:yes"),
                    InlineKeyboardButton("❌ Cancel", callback_data="addorder:confirm:no"),
                ]
            ]
        )
        await msg.reply_text(
            _confirm_text(flow),
            reply_markup=keyboard,
            parse_mode="HTML",
        )


def _confirm_text(flow: dict) -> str:
    return (
        "<b>Confirm registration order</b>\n"
        f"Subnet: SN{flow['netuid']}\n"
        f"Coldkey: {flow['coldkey']}\n"
        f"Hotkey: {flow['hotkey']}\n"
        f"Max price: ≤ {float(flow['max_price']):.6f} TAO\n\n"
        "Ensure wallet is unlocked with /unlock before price hits."
    )


def _cancel_order_button_label(order) -> str:
    p = order.params
    return f"{order.id[:8]} · SN{p.netuid} · {p.coldkey_name}/{p.hotkey_name}"


async def cancelorder_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel an active order — direct id prefix or interactive picker."""
    msg = update.effective_message
    uid = user_id(update)
    args = context.args or []

    if args:
        prefix = args[0]
        orders = await order_service(context).list_active_orders(uid)
        order, err = resolve_order_by_prefix(orders, prefix)
        if err:
            await msg.reply_text(err)
            return
        order = await order_service(context).cancel_order(uid, order.id)
        if order is None:
            await msg.reply_text("Could not cancel order.")
            return
        await msg.reply_text(
            f"🚫 Cancelled order <code>{order.id[:8]}</code>.",
            parse_mode="HTML",
        )
        return

    orders = await order_service(context).list_active_orders(uid)
    if not orders:
        await msg.reply_text("No active orders to cancel.")
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    _cancel_order_button_label(o),
                    callback_data=f"cancelorder:pick:{o.id}",
                )
            ]
            for o in orders
        ]
    )
    await msg.reply_text(
        "Select an active order to cancel:",
        reply_markup=keyboard,
    )


async def cancelorder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await answer_callback(query)
    data = query.data or ""
    uid = user_id(update)

    if data.startswith("cancelorder:pick:"):
        order_id = data.split(":", 2)[2]
        order = await order_service(context).get_order(uid, order_id)
        if order is None or not order.is_active():
            await query.edit_message_text("Order not found or no longer active.")
            return
        p = order.params
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ Yes, cancel",
                        callback_data=f"cancelorder:yes:{order_id}",
                    ),
                    InlineKeyboardButton(
                        "❌ No",
                        callback_data="cancelorder:no",
                    ),
                ]
            ]
        )
        await query.edit_message_text(
            f"<b>Cancel this order?</b>\n"
            f"<code>{order.id[:8]}</code> · SN{p.netuid}\n"
            f"{p.coldkey_name}/{p.hotkey_name} · ≤ {p.max_price_tao:.6f} TAO",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    if data.startswith("cancelorder:yes:"):
        order_id = data.split(":", 2)[2]
        order = await order_service(context).cancel_order(uid, order_id)
        if order is None:
            await query.edit_message_text("Could not cancel order.")
            return
        await query.edit_message_text(
            f"🚫 Cancelled order <code>{order.id[:8]}</code>.",
            parse_mode="HTML",
        )
        return

    if data == "cancelorder:no":
        await query.edit_message_text("Cancelled.")
