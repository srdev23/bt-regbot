from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from sn_reg_bot.handlers.wallets import importhotkey_document, importwallet_document
from sn_reg_bot.handlers.wizard_state import active_document_flow


async def wizard_document_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route uploaded documents to the active import wizard."""
    flow = active_document_flow(context.user_data)
    if flow == "importwallet":
        await importwallet_document(update, context)
    elif flow == "importhotkey":
        await importhotkey_document(update, context)
    elif context.user_data.get("importwallet") or context.user_data.get("importhotkey"):
        await update.effective_message.reply_text(
            "Complete the current step first (e.g. send the wallet/hotkey name before uploading a file)."
        )
