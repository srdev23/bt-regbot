from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters

from regbot.chain import ChainConnectionError, ChainService
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


_CHAIN_HEALTH_INTERVAL_SEC = 300


async def _error_handler(update: object, context) -> None:
    exc = context.error
    if isinstance(exc, ChainConnectionError):
        logger.warning("Chain connection error (update=%s): %s", update, exc)
        if isinstance(update, Update) and update.effective_message:
            try:
                await update.effective_message.reply_text(
                    "⚠️ Bittensor chain temporarily unavailable. "
                    "The bot reconnects automatically — please try again shortly."
                )
            except Exception:
                pass
        return
    logger.exception("Unhandled bot error (update=%s)", update, exc_info=exc)


async def _chain_health_loop(chain: ChainService) -> None:
    while True:
        await asyncio.sleep(_CHAIN_HEALTH_INTERVAL_SEC)
        try:
            await chain.health_check()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Chain health check failed; attempting reconnect", exc_info=True)
            try:
                await chain.reconnect()
            except Exception:
                logger.exception("Chain reconnect after health check failed")


def build_application(settings: Settings) -> Application:
    store = OrderStore(settings.orders_dir)
    chain = ChainService(settings.bittensor_network)
    wallets = WalletSessionManager()
    notifier = TelegramNotifier(chat_min_interval_sec=settings.telegram_chat_min_interval_sec)
    monitor = OrderMonitor(
        store=store,
        chain=chain,
        wallets=wallets,
        wallet_path=str(settings.wallet_path),
        slippage=settings.slippage_tolerance,
        price_notify_interval_sec=settings.price_notify_interval_sec,
        history_keep_limit=settings.order_history_limit,
        notifier=notifier,
    )

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .concurrent_updates(False)
        .post_init(_make_post_init(settings, chain, monitor, store))
        .post_shutdown(_make_post_shutdown(chain, monitor))
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


def _make_post_init(settings: Settings, chain: ChainService, monitor: OrderMonitor, store: OrderStore):
    async def post_init(app: Application) -> None:
        await chain.connect()
        app.bot_data["chain_health_task"] = asyncio.create_task(
            _chain_health_loop(chain), name="chain-health"
        )
        if settings.order_auto_purge:
            removed = store.purge_excess_history(keep=settings.order_history_limit)
            if removed:
                logger.info(
                    "Auto-purged %d old order file(s); keeping %d recent inactive per user",
                    removed,
                    settings.order_history_limit,
                )
        await monitor.start()
        logger.info("Bot started; restored %d active orders", monitor.active_count)

    return post_init


def _make_post_shutdown(chain: ChainService, monitor: OrderMonitor):
    async def post_shutdown(app: Application) -> None:
        health = app.bot_data.pop("chain_health_task", None)
        if health is not None:
            health.cancel()
            await asyncio.gather(health, return_exceptions=True)
        await monitor.stop()
        await chain.close()
        logger.info("Bot stopped")

    return post_shutdown
