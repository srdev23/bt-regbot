from __future__ import annotations

from telegram.ext import ContextTypes

WIZARD_KEYS = (
    "createwallet",
    "importwallet",
    "importhotkey",
    "createhotkey",
    "addorder",
    "editorder",
    "unlock_wallet",
)


def clear_all_wizards(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear every wizard/conversation flow from user_data."""
    for key in WIZARD_KEYS:
        context.user_data.pop(key, None)


def clear_other_wizards(context: ContextTypes.DEFAULT_TYPE, active: str) -> None:
    """Clear all wizard flows except the one being started."""
    for key in WIZARD_KEYS:
        if key != active:
            context.user_data.pop(key, None)


def active_text_flow(user_data: dict) -> str | None:
    """Return which wizard should receive the next text message, if any."""
    if user_data.get("unlock_wallet"):
        return "unlock"

    createhotkey = user_data.get("createhotkey") or {}
    if createhotkey.get("step") == "password":
        return "createhotkey_password"

    importwallet = user_data.get("importwallet") or {}
    if importwallet.get("step") in ("name", "mnemonic", "password"):
        return "importwallet"

    createwallet = user_data.get("createwallet") or {}
    if createwallet.get("step") in ("name", "password"):
        return "createwallet"

    importhotkey = user_data.get("importhotkey") or {}
    if importhotkey.get("step") in ("hotkey_name", "mnemonic"):
        return "importhotkey"

    addorder = user_data.get("addorder") or {}
    if addorder.get("step") in ("netuid", "price"):
        return "addorder"

    editorder = user_data.get("editorder") or {}
    if editorder.get("step") == "price":
        return "editorder"

    if createhotkey.get("step") == "hotkey_name":
        return "createhotkey"

    return None


def active_document_flow(user_data: dict) -> str | None:
    """Return which wizard should receive the next document upload, if any."""
    importwallet = user_data.get("importwallet") or {}
    if importwallet.get("step") == "await_file":
        return "importwallet"

    importhotkey = user_data.get("importhotkey") or {}
    if importhotkey.get("step") == "await_file":
        return "importhotkey"

    return None
