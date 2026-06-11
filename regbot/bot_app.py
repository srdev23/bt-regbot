from __future__ import annotations

import logging

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from regbot.chain import ChainService
from regbot.config import Settings
from regbot.handlers.add_order import (
    addorder_callback,
    addorder_cancel,
    addorder_message,
    addorder_start,
)
from regbot.handlers.commands import (
    cancelorder_cmd,
    clearorders_cmd,
    help_cmd,
    order_cmd,
    orders_cmd,
    price_cmd,
    start_cmd,
    status_cmd,
    unlock_cmd,
    unlock_password_msg,
    wallets_cmd,
)
from regbot.monitor import OrderMonitor
from regbot.notifier import TelegramNotifier
from regbot.order_store import OrderStore
from regbot.wallet_session import WalletSessionManager

logger = logging.getLogger(__name__)


async def _error_handler(update: object, context) -> None:
    logger.exception("Unhandled bot error (update=%s)", update, exc_info=context.error)


def build_application(settings: Settings) -> Application:
    store = OrderStore(settings.orders_dir)
    chain = ChainService(settings.bittensor_network)
    wallets = WalletSessionManager()
    notifier = TelegramNotifier()
    monitor = OrderMonitor(
        store=store,
        chain=chain,
        wallets=wallets,
        wallet_path=str(settings.wallet_path),
        slippage=settings.slippage_tolerance,
        price_notify_interval_sec=settings.price_notify_interval_sec,
        notifier=notifier,
    )

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .concurrent_updates(False)
        .post_init(_make_post_init(settings, chain, monitor, store, notifier))
        .post_shutdown(_make_post_shutdown(chain, monitor, notifier))
        .build()
    )

    app.bot_data["settings"] = settings
    app.bot_data["store"] = store
    app.bot_data["chain"] = chain
    app.bot_data["wallets"] = wallets
    app.bot_data["monitor"] = monitor
    app.bot_data["notifier"] = notifier

    notifier.bind(app)

    app.add_handler(CallbackQueryHandler(addorder_callback), group=-1)
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("wallets", wallets_cmd))
    app.add_handler(CommandHandler("price", price_cmd))
    app.add_handler(CommandHandler("orders", orders_cmd))
    app.add_handler(CommandHandler("order", order_cmd))
    app.add_handler(CommandHandler("clearorders", clearorders_cmd))
    app.add_handler(CommandHandler("cancelorder", cancelorder_cmd))
    app.add_handler(CommandHandler("unlock", unlock_cmd))
    app.add_handler(CommandHandler("addorder", addorder_start))
    app.add_handler(CommandHandler("cancel", addorder_cancel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, addorder_message), group=0)
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, unlock_password_msg),
        group=1,
    )
    app.add_error_handler(_error_handler)

    return app


def _make_post_init(settings: Settings, chain: ChainService, monitor: OrderMonitor, store: OrderStore, notifier: TelegramNotifier):
    async def post_init(app: Application) -> None:
        await chain.connect()
        if settings.order_auto_purge:
            removed = store.purge_stale_globally(settings.order_history_days)
            if removed:
                logger.info("Auto-purged %d stale orders (>%d days)", removed, settings.order_history_days)
        await monitor.start()
        logger.info("Bot started; restored %d active orders", len(monitor._tasks))

    return post_init


def _make_post_shutdown(chain: ChainService, monitor: OrderMonitor, notifier: TelegramNotifier):
    async def post_shutdown(app: Application) -> None:
        await monitor.stop()
        await chain.close()
        await notifier.shutdown()
        logger.info("Bot stopped")

    return post_shutdown
