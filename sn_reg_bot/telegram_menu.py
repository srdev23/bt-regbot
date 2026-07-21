from __future__ import annotations

import logging

from telegram import Bot, BotCommand

logger = logging.getLogger(__name__)

# Shown in the Telegram Menu (/) button. Descriptions: 3–256 chars.
BOT_MENU_COMMANDS: list[BotCommand] = [
    BotCommand("start", "Welcome message"),
    BotCommand("help", "List all commands"),
    # Wallets
    BotCommand("wallets", "List wallets, balances, and addresses"),
    BotCommand("wallet", "Coldkey status (address + hotkeys)"),
    BotCommand("address", "Copy coldkey address (fund / tao.app)"),
    BotCommand("createwallet", "Create a new coldkey"),
    BotCommand("importwallet", "Import coldkey (mnemonic or file)"),
    BotCommand("createhotkey", "Create a hotkey on a coldkey"),
    BotCommand("importhotkey", "Import hotkey (mnemonic or file)"),
    BotCommand("unlock", "Unlock a wallet for signing"),
    BotCommand("removewallet", "Remove a wallet"),
    # Orders
    BotCommand("addorder", "Create a registration order"),
    BotCommand("orders", "List your active orders"),
    BotCommand("order", "Order details by id prefix"),
    BotCommand("editorder", "Edit max price on an active order"),
    BotCommand("cancelorder", "Cancel an active order"),
    # Info
    BotCommand("price", "Registration price for a subnet"),
    BotCommand("status", "Bot and monitor status"),
    BotCommand("cancel", "Cancel current wizard"),
]


async def setup_bot_menu_commands(bot: Bot) -> None:
    """Register commands for the Telegram Menu (/) button."""
    await bot.set_my_commands(BOT_MENU_COMMANDS)
    logger.info("Registered %d menu commands", len(BOT_MENU_COMMANDS))
