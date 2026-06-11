from __future__ import annotations

from telegram.ext import ContextTypes

from regbot.chain import ChainService
from regbot.config import Settings
from regbot.monitor import OrderMonitor
from regbot.notifier import TelegramNotifier
from regbot.order_store import OrderStore
from regbot.wallet_session import WalletSessionManager


def notifier(context: ContextTypes.DEFAULT_TYPE) -> TelegramNotifier:
    return context.application.bot_data["notifier"]


def settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def chain(context: ContextTypes.DEFAULT_TYPE) -> ChainService:
    return context.application.bot_data["chain"]


def store(context: ContextTypes.DEFAULT_TYPE) -> OrderStore:
    return context.application.bot_data["store"]


def monitor(context: ContextTypes.DEFAULT_TYPE) -> OrderMonitor:
    return context.application.bot_data["monitor"]


def wallet_path(context: ContextTypes.DEFAULT_TYPE) -> str:
    return str(settings(context).wallet_path)


def wallets(context: ContextTypes.DEFAULT_TYPE) -> WalletSessionManager:
    return context.application.bot_data["wallets"]
