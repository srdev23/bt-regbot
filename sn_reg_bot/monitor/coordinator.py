from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from sn_reg_bot.chain.service import ChainConnectionError, ChainService
from sn_reg_bot.config import Settings
from sn_reg_bot.database.repository import OrderRepository, UserRepository
from sn_reg_bot.formatting import esc, price_update_message
from sn_reg_bot.models.errors import FailureKind, classify_registration_failure
from sn_reg_bot.models.orders import Order, OrderStatus, OrderType, iso, parse_iso, utc_now
from sn_reg_bot.monitor.price_cache import SubnetPriceCache
from sn_reg_bot.notifier import TelegramNotifier
from sn_reg_bot.services.owner_alerts import OwnerAlertService
from sn_reg_bot.wallet.session import UserWalletSessionManager

logger = logging.getLogger(__name__)


class OrderMonitorCoordinator:
    """Centralized order monitor — one tick loop, shared subnet price fetches."""

    def __init__(
        self,
        *,
        settings: Settings,
        orders: OrderRepository,
        users: UserRepository,
        chain: ChainService,
        sessions: UserWalletSessionManager,
        price_cache: SubnetPriceCache,
        notifier: TelegramNotifier,
        owner_alerts: OwnerAlertService,
    ) -> None:
        self.settings = settings
        self.orders = orders
        self.users = users
        self.chain = chain
        self.sessions = sessions
        self.price_cache = price_cache
        self.notifier = notifier
        self.owner_alerts = owner_alerts
        self._loop_task: asyncio.Task | None = None
        self._registering: set[tuple[int, str, str]] = set()
        self._wallet_lock_notified: set[str] = set()
        self._active_count = 0

    @property
    def active_count(self) -> int:
        return self._active_count

    def _is_owner_user(self, user_id: int) -> bool:
        return (
            self.settings.bot_owner_id is not None
            and user_id == self.settings.bot_owner_id
        )

    async def _user_is_active(self, user_id: int) -> bool:
        if self._is_owner_user(user_id):
            return True
        return await self.users.is_approved(user_id)

    def _prioritize_orders(self, orders: list[Order]) -> list[Order]:
        if self.settings.bot_owner_id is None:
            return orders
        owner_id = self.settings.bot_owner_id
        return sorted(
            orders,
            key=lambda o: (0 if o.user_id == owner_id else 1, o.created_at),
        )

    def _wallet_path(self, user_id: int) -> str:
        return str(self.settings.user_wallet_path(user_id))

    async def _notify_owner_registration_success(self, order: Order, result) -> None:
        params = order.params
        profile = await self.users.get(order.user_id)
        user_label = profile.full_label() if profile else str(order.user_id)
        uid_text = f" UID <b>{result.uid}</b>" if result.uid is not None else ""
        await self.owner_alerts.send(
            (
                f"<b>Registration successful</b>{uid_text}\n"
                f"User: {esc(user_label)} (<code>{order.user_id}</code>)\n"
                f"SN{params.netuid} · {esc(params.coldkey_name)}/{esc(params.hotkey_name)}\n"
                f"Paid: {result.paid_tao:.6f} TAO\n"
                f"Order <code>{order.id[:8]}</code>\n"
                f"Tx: <code>{result.extrinsic_id}</code>"
            ),
            kind=f"reg_ok:{order.id[:8]}",
            force=True,
        )

    def _registration_key(self, order: Order) -> tuple[int, str, str]:
        return (order.user_id, order.params.coldkey_name, order.params.hotkey_name)

    def clear_wallet_lock_notified(self, user_id: int, wallet_name: str) -> None:
        prefix = f"{user_id}:{wallet_name}:"
        self._wallet_lock_notified = {
            k for k in self._wallet_lock_notified if not k.startswith(prefix)
        }

    async def start(self) -> None:
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = asyncio.create_task(self._monitor_loop(), name="order-monitor")
            logger.info("Monitor loop started")

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            await asyncio.gather(self._loop_task, return_exceptions=True)
            self._loop_task = None

    async def track(self, order_id: str) -> None:
        """Order is persisted as active; the central loop picks it up automatically."""
        logger.debug("Tracking order %s", order_id[:8])

    async def untrack(self, order_id: str) -> None:
        order = await self.orders.get(order_id)
        if order and order.price_message_id is not None:
            await self.notifier.delete_message(order.chat_id, order.price_message_id)
            await self.orders.clear_price_message_if_active(order_id)

    async def _monitor_loop(self) -> None:
        try:
            while True:
                try:
                    await self._tick_all()
                except asyncio.CancelledError:
                    raise
                except ChainConnectionError as exc:
                    logger.warning("Chain unavailable during monitor tick: %s", exc)
                    await asyncio.sleep(min(30, self.settings.monitor_tick_sec * 3))
                    continue
                except Exception as exc:
                    logger.exception("Monitor tick failed")
                    await self.owner_alerts.send(
                        f"<b>Monitor tick error</b>\n<code>{esc(str(exc)[:500])}</code>",
                        kind="monitor_tick",
                    )
                await asyncio.sleep(self.settings.monitor_tick_sec)
        except asyncio.CancelledError:
            logger.info("Monitor loop stopped")

    async def _tick_all(self) -> None:
        all_active = await self.orders.list_active()
        active_orders: list[Order] = []
        for order in all_active:
            if await self._user_is_active(order.user_id):
                active_orders.append(order)
        self._active_count = len(active_orders)
        if not active_orders:
            return

        by_netuid: dict[int, list[Order]] = defaultdict(list)
        for order in active_orders:
            if order.order_type == OrderType.SUBNET_REGISTRATION:
                by_netuid[order.params.netuid].append(order)

        netuids = list(by_netuid.keys())
        existence = await asyncio.gather(
            *(self.chain.subnet_exists(n) for n in netuids),
            return_exceptions=True,
        )
        prices: dict[int, float | None] = {}
        for netuid in netuids:
            try:
                price = await self.price_cache.get_price(netuid)
                prices[netuid] = price.burn_tao
            except ChainConnectionError:
                raise
            except Exception as exc:
                logger.warning("Price fetch failed for SN%d: %s", netuid, exc)
                prices[netuid] = None

        for netuid, orders, exists in zip(netuids, [by_netuid[n] for n in netuids], existence):
            if isinstance(exists, Exception) or not exists:
                for order in orders:
                    await self._fail_order(
                        order,
                        f"Subnet {netuid} does not exist",
                        notify=f"❌ Order <code>{order.id[:8]}</code> failed: subnet SN{netuid} not found.",
                    )
                continue
            price_tao = prices.get(netuid)
            if price_tao is None:
                continue
            prioritized = self._prioritize_orders(orders)
            for order in prioritized:
                await self._process_order(order, price_tao)

    async def _process_order(self, order: Order, price_tao: float) -> None:
        fresh = await self.orders.get(order.id)
        if fresh is None or not fresh.is_active():
            return
        order = fresh

        params = order.params
        await self.orders.update_price_if_active(
            order.id, price_tao, iso(utc_now())
        )

        if await self._complete_if_already_registered(order):
            await self._remove_price_message(order)
            return

        if price_tao <= params.max_price_tao:
            reg_key = self._registration_key(order)
            if reg_key in self._registering:
                return
            logger.info(
                "Target hit order=%s user=%s SN%d wallet=%s/%s price=%.6f target=%.6f",
                order.id[:8],
                order.user_id,
                params.netuid,
                params.coldkey_name,
                params.hotkey_name,
                price_tao,
                params.max_price_tao,
            )
            finished = await self._attempt_registration(order, price_tao)
            if finished:
                await self._remove_price_message(order)
            return

        if self._should_notify_price(order):
            await self._notify_price(order)

    async def _complete_if_already_registered(self, order: Order) -> bool:
        """Mark order completed when hotkey is already on subnet (e.g. after stale revive)."""
        params = order.params
        wallet_path = self._wallet_path(order.user_id)
        try:
            from bittensor_wallet import Wallet

            wallet = Wallet(
                name=params.coldkey_name,
                hotkey=params.hotkey_name,
                path=wallet_path,
            )
            if not await self.chain.is_hotkey_registered(params.netuid, wallet):
                return False
        except Exception as exc:
            logger.debug(
                "Could not check registration for order %s: %s", order.id[:8], exc
            )
            return False

        completed = await self.orders.complete_if_active(order.id)
        if not completed:
            return True
        await self.notifier.send(
            order.chat_id,
            f"✅ Order <code>{order.id[:8]}</code>: hotkey <b>{params.hotkey_name}</b> "
            f"is already registered on SN{params.netuid}. Order closed.",
        )
        return True

    def _notify_phase_sec(self, order_id: str) -> int:
        spread = max(1, min(30, self.settings.price_notify_interval_sec // 2))
        return int(order_id[:8], 16) % spread

    def _should_notify_price(self, order: Order) -> bool:
        if order.last_price_notify_at is None:
            created_elapsed = (utc_now() - parse_iso(order.created_at)).total_seconds()
            return created_elapsed >= self._notify_phase_sec(order.id)
        elapsed = (utc_now() - parse_iso(order.last_price_notify_at)).total_seconds()
        return elapsed >= self.settings.price_notify_interval_sec

    async def _notify_price(self, order: Order) -> None:
        params = order.params
        balance = await self.chain.get_coldkey_balance_tao(
            params.coldkey_name,
            self._wallet_path(order.user_id),
            params.hotkey_name,
        )
        message_id = await self.notifier.refresh_price_message(
            order.chat_id,
            price_update_message(order, balance),
            old_message_id=order.price_message_id,
        )
        if message_id is None:
            return
        now = iso(utc_now())
        await self.orders.update_notify_if_active(
            order.id,
            price_message_id=message_id,
            last_price_notify_at=now,
        )

    async def _remove_price_message(self, order: Order) -> None:
        if order.price_message_id is None:
            return
        await self.notifier.delete_message(order.chat_id, order.price_message_id)
        await self.orders.clear_price_message_if_active(order.id)

    async def _fail_order(self, order: Order, error: str, *, notify: str) -> None:
        await self._remove_price_message(order)
        if not await self.orders.fail_if_active(order.id, error):
            return
        await self.notifier.send(order.chat_id, notify)
        profile = await self.users.get(order.user_id)
        user_label = profile.full_label() if profile else str(order.user_id)
        await self.owner_alerts.send(
            (
                f"<b>Order failed</b>\n"
                f"User: {esc(user_label)} (<code>{order.user_id}</code>)\n"
                f"Order <code>{order.id[:8]}</code>\n"
                f"{esc(error)}"
            ),
            kind=f"order_fail:{order.id[:8]}",
            force=True,
        )

    async def _attempt_registration(self, order: Order, price_tao: float) -> bool:
        """Returns True when monitoring for this order should stop."""
        params = order.params
        session = self.sessions.for_user(order.user_id)
        wallet_path = self._wallet_path(order.user_id)
        notify_key = f"{order.user_id}:{params.coldkey_name}:{order.id}"

        if not session.has_password(params.coldkey_name):
            await self.orders.set_last_error_if_active(
                order.id,
                f"Wallet locked: unlock {params.coldkey_name} with /unlock",
            )
            if notify_key not in self._wallet_lock_notified:
                self._wallet_lock_notified.add(notify_key)
                await self.notifier.send(
                    order.chat_id,
                    f"🔒 Price hit for order <code>{order.id[:8]}</code> but wallet "
                    f"<b>{params.coldkey_name}</b> is locked. Use /unlock {params.coldkey_name}",
                )
            return False

        reg_key = self._registration_key(order)
        async with session.signing(params.coldkey_name):
            try:
                wallet = await asyncio.to_thread(
                    session.prepare_for_signing,
                    params.coldkey_name,
                    params.hotkey_name,
                    wallet_path,
                )
            except Exception as exc:
                await self.orders.set_last_error_if_active(order.id, str(exc))
                if notify_key not in self._wallet_lock_notified:
                    self._wallet_lock_notified.add(notify_key)
                    await self.notifier.send(
                        order.chat_id,
                        f"🔒 Cannot sign for order <code>{order.id[:8]}</code>: {exc}",
                    )
                return False

            self._wallet_lock_notified.discard(notify_key)

            if await self.chain.is_hotkey_registered(params.netuid, wallet):
                if await self.orders.complete_if_active(order.id):
                    await self.notifier.send(
                        order.chat_id,
                        f"✅ Order <code>{order.id[:8]}</code>: hotkey <b>{params.hotkey_name}</b> "
                        f"is already registered on SN{params.netuid}.",
                    )
                return True

            fresh_price = await self.price_cache.get_price(params.netuid, force=True)
            if fresh_price.burn_tao > params.max_price_tao:
                logger.info(
                    "Price rose before submit order=%s price=%.6f target=%.6f",
                    order.id[:8],
                    fresh_price.burn_tao,
                    params.max_price_tao,
                )
                return False

            await self.notifier.send(
                order.chat_id,
                f"🎯 Price hit! Submitting registration for SN{params.netuid} "
                f"at {fresh_price.burn_tao:.6f} TAO (target ≤ {params.max_price_tao:.6f}).",
            )

            self._registering.add(reg_key)
            try:
                result = await self.chain.register(
                    wallet,
                    params.netuid,
                    slippage=self.settings.slippage_tolerance,
                    password=session.get_password(params.coldkey_name),
                    target_price=params.max_price_tao,
                )
            finally:
                self._registering.discard(reg_key)

        order = await self.orders.get(order.id) or order
        if not order.is_active():
            return True

        if result.success:
            completed = await self.orders.complete_if_active(
                order.id, last_tx_id=result.extrinsic_id
            )
            if not completed:
                return True
            uid_text = f" UID <b>{result.uid}</b>" if result.uid is not None else ""
            await self.notifier.send(
                order.chat_id,
                f"✅ Registration successful!{uid_text}\n"
                f"SN{params.netuid} · {params.coldkey_name}/{params.hotkey_name}\n"
                f"Paid: {result.paid_tao:.6f} TAO\n"
                f"Tx: <code>{result.extrinsic_id}</code>",
            )
            await self._notify_owner_registration_success(order, result)
            return True

        await self.orders.set_last_error_if_active(order.id, result.message)
        failure_kind = classify_registration_failure(result.message)
        await self.notifier.send(
            order.chat_id,
            f"⚠️ Registration attempt failed for order <code>{order.id[:8]}</code>:\n"
            f"{result.message}",
        )
        if failure_kind == FailureKind.UNRECOVERABLE:
            if await self.orders.fail_if_active(order.id, result.message):
                logger.error(
                    "Order %s marked failed (unrecoverable): %s",
                    order.id[:8],
                    result.message,
                )
                profile = await self.users.get(order.user_id)
                user_label = profile.full_label() if profile else str(order.user_id)
                await self.owner_alerts.send(
                    (
                        f"<b>Registration failed (unrecoverable)</b>\n"
                        f"User: {esc(user_label)} (<code>{order.user_id}</code>)\n"
                        f"Order <code>{order.id[:8]}</code>\n"
                        f"{esc(result.message)}"
                    ),
                    kind=f"reg_fail:{order.id[:8]}",
                    force=True,
                )
            return True

        if "password" in result.message.lower():
            self._wallet_lock_notified.discard(notify_key)
        logger.info("Registration will retry order=%s: %s", order.id[:8], result.message)
        return False
