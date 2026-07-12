from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from tempfile import NamedTemporaryFile

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from sn_reg_bot.formatting import fmt_lock_status, fmt_tao
from sn_reg_bot.handlers.common import chat_id, user_id, wallet_service
from sn_reg_bot.handlers.progress import (
    progress_message,
    run_blocking_with_progress,
    run_blocking_with_transient_status,
    run_with_progress,
    transient_status,
)
from sn_reg_bot.handlers.tg_helpers import answer_callback, safe_edit_message_text
from sn_reg_bot.handlers.wizard_state import clear_all_wizards, clear_other_wizards

logger = logging.getLogger(__name__)

_WALLET_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,32}$")


def _clear_flow(context: ContextTypes.DEFAULT_TYPE, key: str) -> None:
    context.user_data.pop(key, None)

async def wallets_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = user_id(update)
    msg = update.effective_message

    async def _load():
        return await wallet_service(context).list_wallets_with_balances(uid)

    rows = await run_with_progress(msg, _load, text="⏳ Fetching wallets and balances...")
    if not rows:
        await msg.reply_text("No wallets yet. Use /createwallet or /importwallet.")
        return
    lines = []
    for entry in rows:
        bal = fmt_tao(entry.balance)
        hk = ", ".join(entry.hotkeys) if entry.hotkeys else "(no hotkeys)"
        lock = fmt_lock_status(entry.lock_status)
        lines.append(f"• <b>{entry.name}</b> — {bal} — {lock}\n  Hotkeys: {hk}")
    await msg.reply_text(
        "<b>Your wallets</b>\n" + "\n\n".join(lines),
        parse_mode="HTML",
    )


async def wallet_status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /wallet <coldkey_name>")
        return
    name = context.args[0]
    uid = user_id(update)
    msg = update.effective_message

    async def _load():
        return await wallet_service(context).get_coldkey_status(uid, name)

    try:
        statuses, lock_status = await run_with_progress(
            msg, _load, text="⏳ Loading wallet status from chain..."
        )
    except (PermissionError, FileNotFoundError) as exc:
        await msg.reply_text(str(exc))
        return

    lock = fmt_lock_status(lock_status)
    if not statuses:
        await msg.reply_text(
            f"<b>Coldkey {name}</b> — {lock}\nNo hotkeys yet. Use /createhotkey or /importhotkey.",
            parse_mode="HTML",
        )
        return

    lines = [f"<b>Coldkey {name}</b> — {lock}"]
    for st in statuses:
        nets = ", ".join(f"SN{n}" for n in st.registered_netuids) or "none"
        lines.append(
            f"• <b>{st.hotkey_name}</b>\n"
            f"  SS58: <code>{st.hotkey_ss58}</code>\n"
            f"  Registered: {nets}"
        )
    await msg.reply_text("\n".join(lines), parse_mode="HTML")


async def _start_unlock_wallet(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    wallet_name: str,
) -> None:
    uid = user_id(update)
    cid = chat_id(update)
    ws = wallet_service(context)
    bot = context.bot

    if not ws.manager_for(uid).coldkey_exists(wallet_name):
        await bot.send_message(
            chat_id=cid,
            text=f"Wallet <b>{wallet_name}</b> not found.",
            parse_mode="HTML",
        )
        return

    wait_msg = await bot.send_message(
        chat_id=cid,
        text=f"⏳ Unlocking <b>{wallet_name}</b>...",
        parse_mode="HTML",
    )

    try:
        ok = await ws.try_unlock_passwordless_async(uid, wallet_name)
    except (PermissionError, FileNotFoundError) as exc:
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await bot.send_message(chat_id=cid, text=str(exc))
        return

    try:
        await wait_msg.delete()
    except Exception:
        pass

    if ok:
        monitor = context.application.bot_data["monitor"]
        monitor.clear_wallet_lock_notified(uid, wallet_name)
        await bot.send_message(
            chat_id=cid,
            text=f"✅ Wallet <b>{wallet_name}</b> unlocked (no password required).",
            parse_mode="HTML",
        )
        return

    context.user_data["unlock_wallet"] = wallet_name
    clear_other_wizards(context, "unlock_wallet")
    await bot.send_message(
        chat_id=cid,
        text=(
            f"Send the password for wallet <b>{wallet_name}</b>.\n"
            "Your message will be deleted immediately."
        ),
        parse_mode="HTML",
    )


async def unlock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = user_id(update)
    ws = wallet_service(context)

    if not context.args:
        wallets = ws.manager_for(uid).list_wallets()
        if not wallets:
            await update.effective_message.reply_text(
                "No wallets found. Create or import a coldkey first."
            )
            return
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(w.name, callback_data=f"unlock:wallet:{w.name}")]
                for w in wallets
            ]
        )
        await update.effective_message.reply_text(
            "Select wallet to unlock:",
            reply_markup=keyboard,
        )
        return

    await _start_unlock_wallet(update, context, context.args[0])


async def unlock_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await answer_callback(query)
    data = query.data or ""
    if not data.startswith("unlock:wallet:"):
        return
    wallet_name = data.split(":", 2)[2]
    await query.edit_message_text(
        f"⏳ Preparing unlock for <b>{wallet_name}</b>...",
        parse_mode="HTML",
    )
    await _start_unlock_wallet(update, context, wallet_name)


async def lock_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Forget in-memory password and remove persisted secret for a wallet."""
    uid = user_id(update)
    ws = wallet_service(context)

    if not context.args:
        wallets = ws.manager_for(uid).list_wallets()
        unlocked = [w for w in wallets if ws.get_lock_status(uid, w.name) == "unlocked"]
        if not unlocked:
            await update.effective_message.reply_text("No unlocked wallets to lock.")
            return
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(w.name, callback_data=f"lock:wallet:{w.name}")]
                for w in unlocked
            ]
        )
        await update.effective_message.reply_text(
            "Select wallet to lock:",
            reply_markup=keyboard,
        )
        return

    try:
        await ws.lock_wallet(uid, context.args[0])
    except (PermissionError, FileNotFoundError) as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await update.effective_message.reply_text(
        f"🔒 Wallet <b>{context.args[0]}</b> locked.",
        parse_mode="HTML",
    )


async def lock_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await answer_callback(query)
    data = query.data or ""
    if not data.startswith("lock:wallet:"):
        return
    wallet_name = data.split(":", 2)[2]
    uid = user_id(update)
    try:
        await wallet_service(context).lock_wallet(uid, wallet_name)
    except (PermissionError, FileNotFoundError) as exc:
        await query.edit_message_text(str(exc))
        return
    await query.edit_message_text(
        f"🔒 Wallet <b>{wallet_name}</b> locked.",
        parse_mode="HTML",
    )


async def unlock_password_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    wallet_name = context.user_data.pop("unlock_wallet", None)
    if wallet_name is None:
        return
    password = update.effective_message.text or ""
    uid = user_id(update)
    cid = chat_id(update)
    try:
        await update.effective_message.delete()
    except Exception:
        pass

    wait_msg = await context.bot.send_message(
        chat_id=cid,
        text=f"⏳ Unlocking <b>{wallet_name}</b>...",
        parse_mode="HTML",
    )

    try:
        hotkeys = await wallet_service(context).unlock_wallet_async(
            uid, wallet_name, password
        )
        try:
            await wait_msg.delete()
        except Exception:
            pass
        monitor = context.application.bot_data["monitor"]
        monitor.clear_wallet_lock_notified(uid, wallet_name)
        await context.bot.send_message(
            chat_id=cid,
            text=f"✅ Wallet <b>{wallet_name}</b> unlocked ({len(hotkeys)} hotkey(s)).",
            parse_mode="HTML",
        )
    except Exception as exc:
        try:
            await wait_msg.delete()
        except Exception:
            pass
        await context.bot.send_message(chat_id=cid, text=f"❌ Unlock failed: {exc}")


# --- Create wallet wizard ---

async def createwallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_other_wizards(context, "createwallet")
    context.user_data["createwallet"] = {"step": "name"}
    await update.effective_message.reply_text(
        "Create a new coldkey.\nSend the wallet name (letters, numbers, _ and - only):"
    )


async def importwallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_other_wizards(context, "importwallet")
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Mnemonic", callback_data="importwallet:mnemonic")],
            [InlineKeyboardButton("Wallet file (ZIP)", callback_data="importwallet:file")],
        ]
    )
    await update.effective_message.reply_text(
        "Import a coldkey. Choose method:", reply_markup=keyboard
    )


async def createhotkey_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_other_wizards(context, "createhotkey")
    uid = user_id(update)
    wallets = wallet_service(context).manager_for(uid).list_wallets()
    if not wallets:
        await update.effective_message.reply_text("No coldkeys found. Create one first.")
        return
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(w.name, callback_data=f"createhotkey:coldkey:{w.name}")] for w in wallets]
    )
    context.user_data["createhotkey"] = {"step": "coldkey"}
    await update.effective_message.reply_text("Select coldkey:", reply_markup=keyboard)


async def wizard_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_all_wizards(context)
    await update.effective_message.reply_text("Cancelled.")


async def importwallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await answer_callback(query)
    method = query.data.split(":", 1)[1]
    clear_other_wizards(context, "importwallet")
    context.user_data["importwallet"] = {"step": "name", "method": method}
    await query.edit_message_text("Send the wallet name for import:")


async def createhotkey_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await answer_callback(query)
    coldkey = query.data.split(":", 2)[2]
    flow = context.user_data.setdefault("createhotkey", {})
    flow["coldkey"] = coldkey
    flow["step"] = "hotkey_name"
    await query.edit_message_text(f"Coldkey: <b>{coldkey}</b>\nSend new hotkey name:", parse_mode="HTML")


async def createwallet_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow = context.user_data.get("createwallet")
    if not flow:
        return
    text = (update.effective_message.text or "").strip()
    uid = user_id(update)
    ws = wallet_service(context)

    if flow["step"] == "name":
        if not _WALLET_NAME_RE.match(text):
            await update.effective_message.reply_text("Invalid name. Use 1-32 chars: letters, numbers, _ -")
            return
        if ws.manager_for(uid).coldkey_exists(text):
            flow["name"] = text
            flow["step"] = "confirm_overwrite"
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Yes, overwrite", callback_data="createwallet:overwrite:yes"),
                        InlineKeyboardButton("Cancel", callback_data="createwallet:overwrite:no"),
                    ]
                ]
            )
            await update.effective_message.reply_text(
                f"Wallet <b>{text}</b> already exists. Overwrite?",
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            return
        flow["name"] = text
        flow["step"] = "password"
        await update.effective_message.reply_text(
            "Send a password to encrypt the coldkey.\n"
            "Your message will be deleted immediately."
        )
        return

    if flow["step"] == "password":
        try:
            await update.effective_message.delete()
        except Exception:
            pass
        flow["password"] = text
        try:
            async with transient_status(
                context.bot, chat_id(update), "⏳ Creating coldkey..."
            ):
                result = await ws.create_coldkey(
                    uid, flow["name"], text, overwrite=flow.get("overwrite", False)
                )
            _clear_flow(context, "createwallet")
            await context.bot.send_message(
                chat_id=chat_id(update),
                text=(
                    f"✅ Created coldkey <b>{result.name}</b>\n"
                    f"SS58: <code>{result.coldkey_ss58}</code>\n\n"
                    "Create a hotkey with /createhotkey before registering."
                ),
                parse_mode="HTML",
            )
        except Exception as exc:
            _clear_flow(context, "createwallet")
            await context.bot.send_message(chat_id=chat_id(update), text=f"❌ Failed: {exc}")


async def createwallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await answer_callback(query)
    flow = context.user_data.get("createwallet")
    if not flow:
        return
    action = query.data.split(":")[-1]
    if action == "no":
        _clear_flow(context, "createwallet")
        await query.edit_message_text("Cancelled.")
        return
    flow["step"] = "password"
    flow["overwrite"] = True
    await query.edit_message_text(
        "Send a password to encrypt the coldkey.\nYour message will be deleted immediately."
    )


async def importwallet_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow = context.user_data.get("importwallet")
    if not flow:
        return
    text = (update.effective_message.text or "").strip()
    uid = user_id(update)
    ws = wallet_service(context)

    if flow["step"] == "name":
        if not _WALLET_NAME_RE.match(text):
            await update.effective_message.reply_text("Invalid name.")
            return
        if ws.manager_for(uid).coldkey_exists(text):
            flow["name"] = text
            flow["step"] = "confirm_overwrite"
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Yes, overwrite", callback_data="importwallet:overwrite:yes"),
                        InlineKeyboardButton("Cancel", callback_data="importwallet:overwrite:no"),
                    ]
                ]
            )
            await update.effective_message.reply_text(
                f"Wallet <b>{text}</b> exists. Overwrite?",
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            return
        flow["name"] = text
        if flow.get("method") == "mnemonic":
            flow["step"] = "mnemonic"
            await update.effective_message.reply_text(
                "Send your mnemonic (12 or 24 words).\nYour message will be deleted immediately."
            )
        else:
            flow["step"] = "await_file"
            await update.effective_message.reply_text("Upload a ZIP file containing your wallet folder.")
        return

    if flow["step"] == "mnemonic":
        try:
            await update.effective_message.delete()
        except Exception:
            pass
        flow["mnemonic"] = text
        flow["step"] = "password"
        await context.bot.send_message(
            chat_id=chat_id(update),
            text="Send encryption password for the imported coldkey.\nYour message will be deleted.",
        )
        return

    if flow["step"] == "password":
        try:
            await update.effective_message.delete()
        except Exception:
            pass
        try:
            async with transient_status(
                context.bot, chat_id(update), "⏳ Importing coldkey..."
            ):
                result = await ws.import_coldkey_mnemonic(
                    uid,
                    flow["name"],
                    flow["mnemonic"],
                    text,
                    overwrite=flow.get("overwrite", False),
                )
            _clear_flow(context, "importwallet")
            await context.bot.send_message(
                chat_id=chat_id(update),
                text=(
                    f"✅ Imported coldkey <b>{result.name}</b>\n"
                    f"SS58: <code>{result.coldkey_ss58}</code>"
                ),
                parse_mode="HTML",
            )
        except Exception as exc:
            _clear_flow(context, "importwallet")
            await context.bot.send_message(chat_id=chat_id(update), text=f"❌ Import failed: {exc}")


async def importwallet_callback_overwrite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await answer_callback(query)
    flow = context.user_data.get("importwallet")
    if not flow:
        return
    action = query.data.split(":")[-1]
    if action == "no":
        _clear_flow(context, "importwallet")
        await query.edit_message_text("Cancelled.")
        return
    flow["overwrite"] = True
    if flow.get("method") == "mnemonic":
        flow["step"] = "mnemonic"
        await query.edit_message_text(
            "Send your mnemonic.\nYour message will be deleted immediately."
        )
    else:
        flow["step"] = "await_file"
        await query.edit_message_text("Upload a ZIP file containing your wallet folder.")


async def importwallet_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow = context.user_data.get("importwallet")
    if not flow or flow.get("step") != "await_file":
        return
    doc = update.effective_message.document
    if doc is None:
        return
    uid = user_id(update)
    ws = wallet_service(context)
    msg = update.effective_message
    file = await doc.get_file()
    with NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = Path(tmp.name)
    try:
        async with progress_message(msg, "⏳ Importing wallet from file..."):
            result = await ws.import_wallet_zip(
                uid, tmp_path, flow["name"], overwrite=flow.get("overwrite", False)
            )
        _clear_flow(context, "importwallet")
        await update.effective_message.reply_text(
            f"✅ Imported wallet <b>{result.name}</b>\nSS58: <code>{result.coldkey_ss58}</code>",
            parse_mode="HTML",
        )
    except Exception as exc:
        _clear_flow(context, "importwallet")
        await update.effective_message.reply_text(f"❌ Import failed: {exc}")
    finally:
        tmp_path.unlink(missing_ok=True)


async def createhotkey_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow = context.user_data.get("createhotkey")
    if not flow or flow.get("step") != "hotkey_name":
        return
    hotkey_name = (update.effective_message.text or "").strip()
    if not _WALLET_NAME_RE.match(hotkey_name):
        await update.effective_message.reply_text("Invalid hotkey name.")
        return
    uid = user_id(update)
    ws = wallet_service(context)
    coldkey = flow["coldkey"]
    manager = ws.manager_for(uid)
    if manager.hotkey_exists(coldkey, hotkey_name):
        flow["hotkey_name"] = hotkey_name
        flow["step"] = "confirm_overwrite"
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Yes, overwrite", callback_data="createhotkey:overwrite:yes"),
                    InlineKeyboardButton("Cancel", callback_data="createhotkey:overwrite:no"),
                ]
            ]
        )
        await update.effective_message.reply_text(
            f"Hotkey <b>{hotkey_name}</b> exists. Overwrite?",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return
    session = context.application.bot_data["sessions"].for_user(uid)
    if not session.has_password(coldkey):
        flow["hotkey_name"] = hotkey_name
        flow["step"] = "password"
        await update.effective_message.reply_text(
            f"Send password for coldkey <b>{coldkey}</b>.\nMessage will be deleted.",
            parse_mode="HTML",
        )
        return
    try:
        async with progress_message(update.effective_message, "⏳ Creating hotkey..."):
            result = await ws.create_hotkey(uid, coldkey, hotkey_name)
        _clear_flow(context, "createhotkey")
        await update.effective_message.reply_text(
            f"✅ Created hotkey <b>{result.hotkey_name}</b>\n"
            f"SS58: <code>{result.hotkey_ss58}</code>",
            parse_mode="HTML",
        )
    except Exception as exc:
        _clear_flow(context, "createhotkey")
        await update.effective_message.reply_text(f"❌ Failed: {exc}")


async def createhotkey_overwrite_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await answer_callback(query)
    flow = context.user_data.get("createhotkey")
    if not flow:
        return
    action = query.data.split(":")[-1]
    if action == "no":
        _clear_flow(context, "createhotkey")
        await query.edit_message_text("Cancelled.")
        return
    flow["overwrite"] = True
    uid = user_id(update)
    session = context.application.bot_data["sessions"].for_user(uid)
    if not session.has_password(flow["coldkey"]):
        flow["step"] = "password"
        await query.edit_message_text("Send coldkey password. Message will be deleted.")
        return
    await _finish_create_hotkey(
        user_id(update),
        query.message.chat_id,
        context,
        flow,
        overwrite=True,
    )


async def createhotkey_password_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow = context.user_data.get("createhotkey")
    if not flow or flow.get("step") != "password":
        return
    password = update.effective_message.text or ""
    try:
        await update.effective_message.delete()
    except Exception:
        pass
    flow["password"] = password
    await _finish_create_hotkey(
        user_id(update),
        chat_id(update),
        context,
        flow,
        overwrite=flow.get("overwrite", False),
    )


async def _finish_create_hotkey(
    uid: int,
    cid: int,
    context: ContextTypes.DEFAULT_TYPE,
    flow: dict,
    *,
    overwrite: bool,
) -> None:
    ws = wallet_service(context)
    try:
        async with transient_status(context.bot, cid, "⏳ Creating hotkey..."):
            result = await ws.create_hotkey(
                uid,
                flow["coldkey"],
                flow["hotkey_name"],
                password=flow.get("password"),
                overwrite=overwrite,
            )
        _clear_flow(context, "createhotkey")
        await context.bot.send_message(
            chat_id=cid,
            text=(
                f"✅ Created hotkey <b>{result.hotkey_name}</b>\n"
                f"SS58: <code>{result.hotkey_ss58}</code>"
            ),
            parse_mode="HTML",
        )
    except Exception as exc:
        _clear_flow(context, "createhotkey")
        await context.bot.send_message(chat_id=cid, text=f"❌ Failed: {exc}")


async def removewallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = user_id(update)
    ws = wallet_service(context)
    wallets = ws.manager_for(uid).list_wallets()
    if not wallets:
        await update.effective_message.reply_text("No wallets to remove.")
        return

    if context.args:
        name = context.args[0]
        if not ws.manager_for(uid).coldkey_exists(name):
            await update.effective_message.reply_text(f"Wallet <b>{name}</b> not found.", parse_mode="HTML")
            return
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Yes, remove", callback_data=f"removewallet:yes:{name}"),
                    InlineKeyboardButton("Cancel", callback_data="removewallet:no"),
                ]
            ]
        )
        await update.effective_message.reply_text(
            f"⚠️ Remove wallet <b>{name}</b>?\n"
            "This deletes wallet files from the bot. Cancel active orders first.",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(w.name, callback_data=f"removewallet:pick:{w.name}")] for w in wallets]
    )
    await update.effective_message.reply_text(
        "Select wallet to remove:",
        reply_markup=keyboard,
    )


async def removewallet_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from sn_reg_bot.services.wallet_service import WalletInUseError

    query = update.callback_query
    await answer_callback(query)
    data = query.data

    if data == "removewallet:no":
        await safe_edit_message_text(query, "Cancelled.")
        return

    if data.startswith("removewallet:pick:"):
        name = data.split(":", 2)[2]
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("Yes, remove", callback_data=f"removewallet:yes:{name}"),
                    InlineKeyboardButton("Cancel", callback_data="removewallet:no"),
                ]
            ]
        )
        await safe_edit_message_text(
            query,
            f"⚠️ Remove wallet <b>{name}</b>?\n"
            "This deletes wallet files from the bot. Cancel active orders first.",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    if data.startswith("removewallet:yes:"):
        name = data.split(":", 2)[2]
        uid = user_id(update)
        await safe_edit_message_text(query, "⏳ Removing wallet...")
        try:
            await wallet_service(context).remove_wallet(uid, name)
            await safe_edit_message_text(
                query,
                f"🗑 Wallet <b>{name}</b> removed.",
                parse_mode="HTML",
            )
        except WalletInUseError as exc:
            await safe_edit_message_text(query, f"❌ {exc}")
        except Exception as exc:
            await safe_edit_message_text(query, f"❌ Failed to remove wallet: {exc}")


# --- Import hotkey wizard ---

async def importhotkey_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_other_wizards(context, "importhotkey")
    uid = user_id(update)
    wallets = wallet_service(context).manager_for(uid).list_wallets()
    if not wallets:
        await update.effective_message.reply_text("No coldkeys found. Create one first.")
        return
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton(w.name, callback_data=f"importhotkey:coldkey:{w.name}")] for w in wallets]
    )
    context.user_data["importhotkey"] = {"step": "coldkey"}
    await update.effective_message.reply_text("Select coldkey for hotkey import:", reply_markup=keyboard)


async def importhotkey_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await answer_callback(query)
    data = query.data
    flow = context.user_data.get("importhotkey")
    if not flow:
        return

    if data.startswith("importhotkey:coldkey:"):
        flow["coldkey"] = data.split(":", 2)[2]
        flow["step"] = "method"
        keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Mnemonic", callback_data="importhotkey:method:mnemonic")],
                [InlineKeyboardButton("Hotkey file (ZIP)", callback_data="importhotkey:method:file")],
            ]
        )
        await query.edit_message_text(
            f"Coldkey: <b>{flow['coldkey']}</b>\nChoose import method:",
            reply_markup=keyboard,
            parse_mode="HTML",
        )
        return

    if data.startswith("importhotkey:method:"):
        flow["method"] = data.split(":", 2)[2]
        flow["step"] = "hotkey_name"
        await query.edit_message_text("Send the hotkey name to use for this import:")
        return

    if data.startswith("importhotkey:overwrite:"):
        action = data.split(":")[-1]
        if action == "no":
            _clear_flow(context, "importhotkey")
            await query.edit_message_text("Cancelled.")
            return
        flow["overwrite"] = True
        if flow.get("method") == "mnemonic":
            flow["step"] = "mnemonic"
            await query.edit_message_text(
                "Send your hotkey mnemonic.\nYour message will be deleted immediately."
            )
        else:
            flow["step"] = "await_file"
            await query.edit_message_text(
                "Upload a ZIP containing the hotkey file "
                f"(named <b>{flow['hotkey_name']}</b>).",
                parse_mode="HTML",
            )


async def importhotkey_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow = context.user_data.get("importhotkey")
    if not flow:
        return
    text = (update.effective_message.text or "").strip()
    uid = user_id(update)
    ws = wallet_service(context)
    coldkey = flow["coldkey"]

    if flow["step"] == "hotkey_name":
        if not _WALLET_NAME_RE.match(text):
            await update.effective_message.reply_text("Invalid hotkey name.")
            return
        if ws.manager_for(uid).hotkey_exists(coldkey, text):
            flow["hotkey_name"] = text
            flow["step"] = "confirm_overwrite"
            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("Yes, overwrite", callback_data="importhotkey:overwrite:yes"),
                        InlineKeyboardButton("Cancel", callback_data="importhotkey:overwrite:no"),
                    ]
                ]
            )
            await update.effective_message.reply_text(
                f"Hotkey <b>{text}</b> exists. Overwrite?",
                reply_markup=keyboard,
                parse_mode="HTML",
            )
            return
        flow["hotkey_name"] = text
        if flow.get("method") == "mnemonic":
            flow["step"] = "mnemonic"
            await update.effective_message.reply_text(
                "Send your hotkey mnemonic.\nYour message will be deleted immediately."
            )
        else:
            flow["step"] = "await_file"
            await update.effective_message.reply_text(
                f"Upload a ZIP containing hotkey file <b>{text}</b>.",
                parse_mode="HTML",
            )
        return

    if flow["step"] == "mnemonic":
        try:
            await update.effective_message.delete()
        except Exception:
            pass
        try:
            async with transient_status(
                context.bot, chat_id(update), "⏳ Importing hotkey..."
            ):
                result = await ws.import_hotkey_mnemonic(
                    uid,
                    coldkey,
                    flow["hotkey_name"],
                    text,
                    overwrite=flow.get("overwrite", False),
                )
            _clear_flow(context, "importhotkey")
            await context.bot.send_message(
                chat_id=chat_id(update),
                text=(
                    f"✅ Imported hotkey <b>{result.hotkey_name}</b> on <b>{result.coldkey_name}</b>\n"
                    f"SS58: <code>{result.hotkey_ss58}</code>"
                ),
                parse_mode="HTML",
            )
        except Exception as exc:
            _clear_flow(context, "importhotkey")
            await context.bot.send_message(chat_id=chat_id(update), text=f"❌ Import failed: {exc}")


async def importhotkey_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    flow = context.user_data.get("importhotkey")
    if not flow or flow.get("step") != "await_file":
        return
    doc = update.effective_message.document
    if doc is None:
        return
    uid = user_id(update)
    ws = wallet_service(context)
    msg = update.effective_message
    file = await doc.get_file()
    with NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        await file.download_to_drive(tmp.name)
        tmp_path = Path(tmp.name)
    try:
        async with progress_message(msg, "⏳ Importing hotkey from file..."):
            result = await ws.import_hotkey_zip(
                uid,
                flow["coldkey"],
                flow["hotkey_name"],
                tmp_path,
                overwrite=flow.get("overwrite", False),
            )
        _clear_flow(context, "importhotkey")
        await msg.reply_text(
            f"✅ Imported hotkey <b>{result.hotkey_name}</b>\n"
            f"SS58: <code>{result.hotkey_ss58}</code>",
            parse_mode="HTML",
        )
    except Exception as exc:
        _clear_flow(context, "importhotkey")
        await msg.reply_text(f"❌ Import failed: {exc}")
    finally:
        tmp_path.unlink(missing_ok=True)


