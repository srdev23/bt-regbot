from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from sn_reg_bot.handlers.orders import addorder_message
from sn_reg_bot.handlers.edit_order import editorder_message
from sn_reg_bot.handlers.wallets import (
    createhotkey_message,
    createhotkey_password_msg,
    createwallet_message,
    importhotkey_message,
    importwallet_message,
    unlock_password_msg,
)
from sn_reg_bot.handlers.wizard_state import active_text_flow


async def wizard_text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route text messages to the wizard flow that is actively waiting for input."""
    flow = active_text_flow(context.user_data)
    if flow is None:
        return

    if flow == "unlock":
        await unlock_password_msg(update, context)
    elif flow == "createhotkey_password":
        await createhotkey_password_msg(update, context)
    elif flow == "addorder":
        await addorder_message(update, context)
    elif flow == "editorder":
        await editorder_message(update, context)
    elif flow == "createwallet":
        await createwallet_message(update, context)
    elif flow == "importwallet":
        await importwallet_message(update, context)
    elif flow == "importhotkey":
        await importhotkey_message(update, context)
    elif flow == "createhotkey":
        await createhotkey_message(update, context)
