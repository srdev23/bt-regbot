from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from sn_reg_bot.handlers.common import order_service, user_id
from sn_reg_bot.handlers.order_match import resolve_order_by_prefix
from sn_reg_bot.handlers.tg_helpers import answer_callback
from sn_reg_bot.handlers.wizard_state import clear_other_wizards
from sn_reg_bot.services.order_service import OrderNotEditableError


def _order_button_label(order) -> str:
    p = order.params
    return f"{order.id[:8]} · SN{p.netuid} · ≤{p.max_price_tao:.4f} TAO"


async def _apply_price_edit(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    order_id: str,
    max_price_tao: float,
) -> None:
    msg = update.effective_message
    uid = user_id(update)
    try:
        order = await order_service(context).update_max_price(uid, order_id, max_price_tao)
    except OrderNotEditableError as exc:
        await msg.reply_text(f"❌ {exc}")
        return
    except ValueError as exc:
        await msg.reply_text(f"❌ {exc}")
        return
    except Exception as exc:
        await msg.reply_text(f"❌ Failed to update order: {exc}")
        return

    p = order.params
    await msg.reply_text(
        f"✅ Updated order <code>{order.id[:8]}</code>\n"
        f"SN{p.netuid} · {p.coldkey_name}/{p.hotkey_name}\n"
        f"New max price: ≤ <b>{p.max_price_tao:.6f}</b> TAO",
        parse_mode="HTML",
    )


async def editorder_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Edit max registration price — direct args or interactive picker."""
    msg = update.effective_message
    args = context.args or []

    if len(args) >= 2:
        prefix, price_str = args[0], args[1]
        try:
            max_price = float(price_str)
            if max_price <= 0:
                raise ValueError
        except ValueError:
            await msg.reply_text("Usage: /editorder <order_id_prefix> <new_max_price_tao>")
            return

        uid = user_id(update)
        orders = await order_service(context).list_active_orders(uid)
        order, err = resolve_order_by_prefix(orders, prefix)
        if err:
            await msg.reply_text(err)
            return
        await _apply_price_edit(update, context, order.id, max_price)
        return

    if len(args) == 1:
        await msg.reply_text(
            "Usage: /editorder <order_id_prefix> <new_max_price_tao>\n"
            "Or send /editorder alone to pick an order interactively."
        )
        return

    uid = user_id(update)
    orders = await order_service(context).list_active_orders(uid)
    if not orders:
        await msg.reply_text("No active orders to edit. Use /orders to see all orders.")
        return

    clear_other_wizards(context, "editorder")
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(_order_button_label(o), callback_data=f"editorder:select:{o.id}")]
            for o in orders
        ]
    )
    await msg.reply_text(
        "Select an active order to edit its max registration price:",
        reply_markup=keyboard,
    )


async def editorder_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await answer_callback(query)
    data = query.data or ""

    if data.startswith("editorder:select:"):
        order_id = data.split(":", 2)[2]
        uid = user_id(update)
        order = await order_service(context).get_order(uid, order_id)
        if order is None or not order.is_active():
            await query.edit_message_text("Order not found or no longer active.")
            return

        clear_other_wizards(context, "editorder")
        context.user_data["editorder"] = {"step": "price", "order_id": order_id}
        p = order.params
        await query.edit_message_text(
            f"Order <code>{order.id[:8]}</code> · SN{p.netuid}\n"
            f"{p.coldkey_name}/{p.hotkey_name}\n"
            f"Current max price: ≤ <b>{p.max_price_tao:.6f}</b> TAO\n\n"
            "Send the new max registration price in TAO:",
            parse_mode="HTML",
        )
        return

    if data == "editorder:cancel":
        context.user_data.pop("editorder", None)
        await query.edit_message_text("Cancelled.")


async def editorder_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow = context.user_data.get("editorder")
    if not flow or flow.get("step") != "price":
        return

    text = (update.effective_message.text or "").strip()
    try:
        max_price = float(text)
        if max_price <= 0:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Enter a positive number for max price in TAO.")
        return

    order_id = flow["order_id"]
    context.user_data.pop("editorder", None)
    await _apply_price_edit(update, context, order_id, max_price)
