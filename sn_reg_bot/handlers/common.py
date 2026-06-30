from __future__ import annotations

from typing import TYPE_CHECKING

from telegram.ext import ContextTypes

from sn_reg_bot.config import Settings
from sn_reg_bot.database.repository import OrderRepository, UserRepository

if TYPE_CHECKING:
    from sn_reg_bot.chain.service import ChainService
    from sn_reg_bot.monitor.coordinator import OrderMonitorCoordinator
    from sn_reg_bot.notifier import TelegramNotifier
    from sn_reg_bot.services.access_service import AccessService
    from sn_reg_bot.services.admin_service import AdminService
    from sn_reg_bot.services.order_service import OrderService
    from sn_reg_bot.services.owner_alerts import OwnerAlertService
    from sn_reg_bot.services.wallet_service import WalletService
    from sn_reg_bot.wallet.session import UserWalletSessionManager


def settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def is_bot_owner(context: ContextTypes.DEFAULT_TYPE, telegram_user_id: int) -> bool:
    owner_id = settings(context).bot_owner_id
    return owner_id is not None and telegram_user_id == owner_id


def users_repo(context: ContextTypes.DEFAULT_TYPE) -> UserRepository:
    return context.application.bot_data["users_repo"]


def admin_service(context: ContextTypes.DEFAULT_TYPE) -> AdminService:
    return context.application.bot_data["admin_service"]


def access_service(context: ContextTypes.DEFAULT_TYPE) -> AccessService:
    return context.application.bot_data["access_service"]


def owner_alerts(context: ContextTypes.DEFAULT_TYPE) -> OwnerAlertService:
    return context.application.bot_data["owner_alerts"]


def chain(context: ContextTypes.DEFAULT_TYPE) -> ChainService:
    return context.application.bot_data["chain"]


def orders_repo(context: ContextTypes.DEFAULT_TYPE) -> OrderRepository:
    return context.application.bot_data["orders_repo"]


def wallet_service(context: ContextTypes.DEFAULT_TYPE) -> WalletService:
    return context.application.bot_data["wallet_service"]


def order_service(context: ContextTypes.DEFAULT_TYPE) -> OrderService:
    return context.application.bot_data["order_service"]


def monitor(context: ContextTypes.DEFAULT_TYPE) -> OrderMonitorCoordinator:
    return context.application.bot_data["monitor"]


def sessions(context: ContextTypes.DEFAULT_TYPE) -> UserWalletSessionManager:
    return context.application.bot_data["sessions"]


def notifier(context: ContextTypes.DEFAULT_TYPE) -> TelegramNotifier:
    return context.application.bot_data["notifier"]


def user_id(update) -> int:
    return update.effective_user.id


def chat_id(update) -> int:
    return update.effective_chat.id
