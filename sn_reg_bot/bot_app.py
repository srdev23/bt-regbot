from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    TypeHandler,
    filters,
)

from sn_reg_bot.chain.service import ChainConnectionError, ChainService
from sn_reg_bot.config import Settings
from sn_reg_bot.database.connection import Database
from sn_reg_bot.database.repository import OrderRepository, UserRepository, WalletSecretRepository
from sn_reg_bot.formatting import esc
from sn_reg_bot.handlers.access_gate import guarded, owner_only
from sn_reg_bot.handlers.admin_notify import notify_cmd
from sn_reg_bot.handlers.admin_users import (
    applications_cmd,
    approve_cmd,
    suspend_cmd,
    user_detail_cmd,
    useradmin_callback,
    users_cmd,
)
from sn_reg_bot.handlers.commands import (
    help_cmd,
    order_cmd,
    orders_cmd,
    price_cmd,
    start_cmd,
    status_cmd,
)
from sn_reg_bot.handlers.document_router import wizard_document_router
from sn_reg_bot.handlers.edit_order import editorder_callback, editorder_start
from sn_reg_bot.handlers.orders import (
    addorder_callback,
    addorder_start,
    cancelorder_callback,
    cancelorder_start,
)
from sn_reg_bot.handlers.router import wizard_text_router
from sn_reg_bot.handlers.user_tracking import track_user_update
from sn_reg_bot.handlers.wallets import (
    createhotkey_callback,
    createhotkey_overwrite_callback,
    createhotkey_start,
    createwallet_callback,
    createwallet_start,
    importwallet_callback,
    importwallet_callback_overwrite,
    importwallet_start,
    importhotkey_callback,
    importhotkey_start,
    removewallet_callback,
    removewallet_cmd,
    lock_cmd,
    lock_callback,
    unlock_cmd,
    unlock_callback,
    wallet_status_cmd,
    wallets_cmd,
    wizard_cancel,
)
from sn_reg_bot.monitor.coordinator import OrderMonitorCoordinator
from sn_reg_bot.monitor.price_cache import SubnetPriceCache
from sn_reg_bot.notifier import TelegramNotifier
from sn_reg_bot.services.access_service import AccessService
from sn_reg_bot.services.admin_service import AdminService
from sn_reg_bot.services.order_service import OrderService
from sn_reg_bot.services.owner_alerts import OwnerAlertService
from sn_reg_bot.services.wallet_service import WalletService
from sn_reg_bot.telegram_menu import setup_bot_menu_commands
from sn_reg_bot.wallet.password_store import WalletPasswordStore, build_fernet

from sn_reg_bot.wallet.session import UserWalletSessionManager

logger = logging.getLogger(__name__)

_CHAIN_HEALTH_INTERVAL_SEC = 300


async def _error_handler(update: object, context) -> None:
    exc = context.error
    owner_alerts = context.application.bot_data.get("owner_alerts")
    if isinstance(exc, ChainConnectionError):
        logger.warning("Chain connection error (update=%s): %s", update, exc)
        if owner_alerts is not None:
            await owner_alerts.send(
                f"<b>Chain connection error</b>\n<code>{esc(str(exc)[:500])}</code>",
                kind="chain_error",
            )
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
    if owner_alerts is not None:
        await owner_alerts.send(
            f"<b>Unhandled bot error</b>\n<code>{esc(str(exc)[:500])}</code>",
            kind="unhandled_error",
        )


async def _chain_health_loop(chain: ChainService, owner_alerts: OwnerAlertService) -> None:
    while True:
        await asyncio.sleep(_CHAIN_HEALTH_INTERVAL_SEC)
        try:
            await chain.health_check()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Chain health check failed; attempting reconnect", exc_info=True)
            await owner_alerts.send(
                f"<b>Chain health check failed</b>\n<code>{esc(str(exc)[:500])}</code>",
                kind="chain_health",
            )
            try:
                await chain.reconnect()
            except Exception as reconnect_exc:
                logger.exception("Chain reconnect after health check failed")
                await owner_alerts.send(
                    f"<b>Chain reconnect failed</b>\n<code>{esc(str(reconnect_exc)[:500])}</code>",
                    kind="chain_reconnect",
                )


def build_application(settings: Settings) -> Application:
    db = Database(settings.db_path)
    users_repo = UserRepository(db)
    orders_repo = OrderRepository(db)
    wallet_secrets_repo = WalletSecretRepository(db)
    fernet = build_fernet(settings.wallet_secret_key) if settings.wallet_secret_key else None
    password_store = WalletPasswordStore(wallet_secrets_repo, fernet)
    if settings.wallet_secret_key is None:
        logger.warning(
            "WALLET_SECRET_KEY is not set; wallet passwords will not persist across restarts."
        )
    chain = ChainService(settings.bittensor_network)
    sessions = UserWalletSessionManager()
    price_cache = SubnetPriceCache(chain, settings.price_cache_ttl_sec)
    notifier = TelegramNotifier(chat_min_interval_sec=settings.telegram_chat_min_interval_sec)
    owner_alerts_svc = OwnerAlertService(settings, notifier)
    wallet_svc = WalletService(
        settings, chain, sessions, users_repo, orders_repo, password_store
    )
    monitor = OrderMonitorCoordinator(
        settings=settings,
        orders=orders_repo,
        users=users_repo,
        chain=chain,
        sessions=sessions,
        price_cache=price_cache,
        notifier=notifier,
        owner_alerts=owner_alerts_svc,
    )
    order_svc = OrderService(
        orders_repo,
        users_repo,
        wallet_svc,
        monitor,
    )
    admin_svc = AdminService(settings, users_repo, orders_repo, wallet_svc)
    access_svc = AccessService(settings, users_repo, notifier, owner_alerts_svc)

    app = (
        Application.builder()
        .token(settings.telegram_bot_token)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .concurrent_updates(True)
        .post_init(_make_post_init(db, chain, monitor, owner_alerts_svc, wallet_svc))
        .post_shutdown(_make_post_shutdown(db, chain, monitor, wallet_svc))
        .build()
    )

    app.bot_data["settings"] = settings
    app.bot_data["db"] = db
    app.bot_data["users_repo"] = users_repo
    app.bot_data["orders_repo"] = orders_repo
    app.bot_data["chain"] = chain
    app.bot_data["sessions"] = sessions
    app.bot_data["monitor"] = monitor
    app.bot_data["notifier"] = notifier
    app.bot_data["owner_alerts"] = owner_alerts_svc
    app.bot_data["wallet_service"] = wallet_svc
    app.bot_data["order_service"] = order_svc
    app.bot_data["admin_service"] = admin_svc
    app.bot_data["access_service"] = access_svc

    notifier.bind(app.bot)

    # Profile sync middleware (group -1, must not block command handlers)
    app.add_handler(TypeHandler(Update, track_user_update, block=False), group=-1)

    # Callbacks
    app.add_handler(CallbackQueryHandler(owner_only(useradmin_callback), pattern=r"^useradmin:"))
    app.add_handler(CallbackQueryHandler(guarded(unlock_callback), pattern=r"^unlock:"))
    app.add_handler(CallbackQueryHandler(guarded(lock_callback), pattern=r"^lock:"))
    app.add_handler(CallbackQueryHandler(guarded(cancelorder_callback), pattern=r"^cancelorder:"))
    app.add_handler(CallbackQueryHandler(guarded(addorder_callback), pattern=r"^addorder:"))
    app.add_handler(CallbackQueryHandler(guarded(editorder_callback), pattern=r"^editorder:"))
    app.add_handler(CallbackQueryHandler(guarded(importwallet_callback), pattern=r"^importwallet:(?:mnemonic|file)$"))
    app.add_handler(CallbackQueryHandler(guarded(importwallet_callback_overwrite), pattern=r"^importwallet:overwrite:"))
    app.add_handler(CallbackQueryHandler(guarded(createwallet_callback), pattern=r"^createwallet:overwrite:"))
    app.add_handler(CallbackQueryHandler(guarded(createhotkey_callback), pattern=r"^createhotkey:coldkey:"))
    app.add_handler(CallbackQueryHandler(guarded(createhotkey_overwrite_callback), pattern=r"^createhotkey:overwrite:"))
    app.add_handler(CallbackQueryHandler(guarded(removewallet_callback), pattern=r"^removewallet:"))
    app.add_handler(CallbackQueryHandler(guarded(importhotkey_callback), pattern=r"^importhotkey:"))

    # Commands
    app.add_handler(CommandHandler("start", guarded(start_cmd)))
    app.add_handler(CommandHandler("help", guarded(help_cmd)))
    app.add_handler(CommandHandler("status", guarded(status_cmd)))
    app.add_handler(CommandHandler("applications", owner_only(applications_cmd)))
    app.add_handler(CommandHandler("users", owner_only(users_cmd)))
    app.add_handler(CommandHandler("user", owner_only(user_detail_cmd)))
    app.add_handler(CommandHandler("approve", owner_only(approve_cmd)))
    app.add_handler(CommandHandler("suspend", owner_only(suspend_cmd)))
    app.add_handler(CommandHandler("notify", owner_only(notify_cmd)))
    app.add_handler(CommandHandler("wallets", guarded(wallets_cmd)))
    app.add_handler(CommandHandler("wallet", guarded(wallet_status_cmd)))
    app.add_handler(CommandHandler("price", guarded(price_cmd)))
    app.add_handler(CommandHandler("orders", guarded(orders_cmd)))
    app.add_handler(CommandHandler("order", guarded(order_cmd)))
    app.add_handler(CommandHandler("cancelorder", guarded(cancelorder_start)))
    app.add_handler(CommandHandler("editorder", guarded(editorder_start)))
    app.add_handler(CommandHandler("unlock", guarded(unlock_cmd)))
    app.add_handler(CommandHandler("lock", guarded(lock_cmd)))
    app.add_handler(CommandHandler("createwallet", guarded(createwallet_start)))
    app.add_handler(CommandHandler("importwallet", guarded(importwallet_start)))
    app.add_handler(CommandHandler("createhotkey", guarded(createhotkey_start)))
    app.add_handler(CommandHandler("importhotkey", guarded(importhotkey_start)))
    app.add_handler(CommandHandler("removewallet", guarded(removewallet_cmd)))
    app.add_handler(CommandHandler("addorder", guarded(addorder_start)))
    app.add_handler(CommandHandler("cancel", guarded(wizard_cancel)))

    # Wizards
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, guarded(wizard_text_router)),
        group=0,
    )
    app.add_handler(
        MessageHandler(filters.Document.ALL, guarded(wizard_document_router)),
        group=0,
    )

    app.add_error_handler(_error_handler)
    return app


def _make_post_init(
    db: Database,
    chain: ChainService,
    monitor: OrderMonitorCoordinator,
    owner_alerts: OwnerAlertService,
    wallet_svc: WalletService,
):
    async def post_init(app: Application) -> None:
        await db.connect()
        await chain.connect()
        restored = await wallet_svc.restore_saved_passwords()
        if restored:
            logger.info("Restored %d wallet unlock session(s) after startup", restored)
        await setup_bot_menu_commands(app.bot)
        app.bot_data["chain_health_task"] = asyncio.create_task(
            _chain_health_loop(chain, owner_alerts), name="chain-health"
        )
        await monitor.start()
        logger.info("Bot started; monitor active")

    return post_init


def _make_post_shutdown(
    db: Database,
    chain: ChainService,
    monitor: OrderMonitorCoordinator,
    wallet_svc: WalletService,
):
    async def post_shutdown(app: Application) -> None:
        health = app.bot_data.pop("chain_health_task", None)
        if health is not None:
            health.cancel()
            await asyncio.gather(health, return_exceptions=True)
        await monitor.stop()
        wallet_svc.shutdown()
        await chain.close()
        await db.close()
        logger.info("Bot stopped")

    return post_shutdown
