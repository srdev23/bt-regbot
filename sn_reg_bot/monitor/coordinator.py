from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass

from sn_reg_bot.chain.service import (
    ChainConnectionError,
    ChainService,
    ComposedRegistration,
    NetuidRegistrationSnapshot,
    PreparedSubmission,
    RegistrationResult,
    registration_limit_price,
)
from bittensor_cli.src.bittensor.utils import get_hotkey_pub_ss58
from sn_reg_bot.config import Settings
from sn_reg_bot.database.repository import OrderRepository, UserRepository
from sn_reg_bot.formatting import esc, price_update_message
from sn_reg_bot.models.errors import FailureKind, classify_registration_failure
from sn_reg_bot.models.orders import Order, OrderStatus, OrderType, iso, parse_iso, utc_now
from sn_reg_bot.monitor.batch import group_orders_for_batch
from sn_reg_bot.monitor.price_cache import SubnetPriceCache
from sn_reg_bot.notifier import TelegramNotifier
from sn_reg_bot.services.owner_alerts import OwnerAlertService
from sn_reg_bot.wallet.session import UserWalletSessionManager

logger = logging.getLogger(__name__)


@dataclass
class PendingRegistration:
    netuid: int
    hotkey_ss58: str
    tx_id: str | None
    paid_tao: float | None
    created_at: float


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
        self._price_notify_suppressed: set[str] = set()
        self._price_notify_inflight: set[str] = set()
        self._registration_warm_inflight: set[str] = set()
        self._pending_registrations: dict[str, PendingRegistration] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._balance_cache: dict[tuple[int, str, str], tuple[float, float]] = {}
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
        prices: dict[int, NetuidRegistrationSnapshot | None] = {}
        for netuid in netuids:
            try:
                prices[netuid] = await self.chain.capture_registration_snapshot(netuid)
            except ChainConnectionError:
                raise
            except Exception as exc:
                logger.warning("Snapshot failed for SN%d: %s", netuid, exc)
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
            snapshot = prices.get(netuid)
            if snapshot is None:
                continue
            await self._process_netuid_orders(
                netuid, self._prioritize_orders(orders), snapshot
            )

    async def _process_netuid_orders(
        self, netuid: int, orders: list[Order], snapshot: NetuidRegistrationSnapshot
    ) -> None:
        """Scan orders, register at-target ones immediately, notify others after."""
        price_tao = snapshot.price_tao
        at_target: list[Order] = []
        to_refresh: list[Order] = []
        to_notify: list[Order] = []
        chats_scheduled: set[int] = set()

        for order in orders:
            fresh = await self.orders.get(order.id)
            if fresh is None or not fresh.is_active():
                continue
            order = fresh
            if await self._check_pending_registration(order):
                continue

            prev_price = order.last_price
            await self.orders.update_price_if_active(
                order.id, price_tao, iso(utc_now())
            )

            params = order.params
            if price_tao <= params.max_price_tao:
                reg_key = self._registration_key(order)
                if reg_key not in self._registering:
                    self._price_notify_suppressed.add(order.id)
                    await self._clear_price_alert(order)
                    at_target.append(order)
            else:
                self._schedule_registration_warm(order)
                if await self._complete_if_already_registered(order):
                    pass
                elif order.price_message_id is not None:
                    if self._price_changed(prev_price, price_tao):
                        if (
                            order.id not in self._price_notify_inflight
                            and self._should_replace_price(order)
                            and order.chat_id not in chats_scheduled
                        ):
                            to_refresh.append(order)
                            chats_scheduled.add(order.chat_id)
                elif self._should_notify_price(order):
                    if order.chat_id not in chats_scheduled:
                        to_notify.append(order)
                        chats_scheduled.add(order.chat_id)

        if at_target:
            for target_price, group in group_orders_for_batch(at_target).items():
                if len(group) >= 2:
                    await self._attempt_batch_registration(
                        netuid, group, snapshot, target_price
                    )
                else:
                    order = group[0]
                    logger.info(
                        "Target hit order=%s user=%s SN%d wallet=%s/%s price=%.6f target=%.6f",
                        order.id[:8],
                        order.user_id,
                        netuid,
                        order.params.coldkey_name,
                        order.params.hotkey_name,
                        price_tao,
                        target_price,
                    )
                    finished = await self._attempt_registration(
                        order, snapshot, target_price
                    )
                    if finished:
                        await self._remove_price_message(order)

        for order in to_refresh:
            self._schedule_price_notify(order, snapshot.price_tao)

        for order in to_notify:
            self._schedule_price_notify(order, snapshot.price_tao)

    @staticmethod
    def _price_changed(prev_price: float | None, current_price: float) -> bool:
        if prev_price is None:
            return True
        return abs(prev_price - current_price) >= 1e-9

    def _schedule_registration_warm(self, order: Order) -> None:
        if order.id in self._registration_warm_inflight:
            return
        params = order.params
        session = self.sessions.for_user(order.user_id)
        if (
            not session.has_password(params.coldkey_name)
            or session.has_prepared_wallet(params.coldkey_name, params.hotkey_name)
        ):
            return
        self._registration_warm_inflight.add(order.id)
        task = asyncio.create_task(
            self._warm_registration_cache(order),
            name=f"registration-warm-{order.id[:8]}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(
            lambda _t, oid=order.id: self._registration_warm_inflight.discard(oid)
        )

    async def _warm_registration_cache(self, order: Order) -> None:
        fresh = await self.orders.get(order.id)
        if fresh is None or not fresh.is_active():
            return
        params = fresh.params
        session = self.sessions.for_user(fresh.user_id)
        if not session.has_password(params.coldkey_name):
            return
        wallet_path = self._wallet_path(fresh.user_id)
        try:
            async with session.signing(params.coldkey_name):
                await asyncio.to_thread(
                    session.prepare_for_signing,
                    params.coldkey_name,
                    params.hotkey_name,
                    wallet_path,
                )
            logger.debug(
                "Prepared signing cache order=%s wallet=%s/%s",
                fresh.id[:8],
                params.coldkey_name,
                params.hotkey_name,
            )
        except Exception as exc:
            logger.debug(
                "Could not prepare signing cache order=%s wallet=%s/%s: %s",
                fresh.id[:8],
                params.coldkey_name,
                params.hotkey_name,
                exc,
            )

    async def _check_pending_registration(self, order: Order) -> bool:
        pending = self._pending_registrations.get(order.id)
        if pending is None:
            return False

        uid = await self.chain.lookup_registration_uid(
            pending.netuid, pending.hotkey_ss58, retries=1
        )
        if uid is None and await self.chain.is_hotkey_registered_ss58(
            pending.netuid, pending.hotkey_ss58
        ):
            uid = await self.chain.lookup_registration_uid(pending.netuid, pending.hotkey_ss58)

        if uid is not None:
            self._pending_registrations.pop(order.id, None)
            completed = await self.orders.complete_if_active(
                order.id, last_tx_id=pending.tx_id
            )
            if completed:
                await self._remove_price_message(order)
                params = order.params
                paid = pending.paid_tao if pending.paid_tao is not None else order.last_price
                paid_text = f"{paid:.6f} TAO" if paid is not None else "unknown"
                await self.notifier.send(
                    order.chat_id,
                    f"✅ Registration confirmed! UID <b>{uid}</b>\n"
                    f"SN{params.netuid} · {params.coldkey_name}/{params.hotkey_name}\n"
                    f"Paid: {paid_text}\n"
                    f"Tx: <code>{pending.tx_id}</code>",
                )
                await self._notify_owner_registration_success(
                    order,
                    RegistrationResult(
                        success=True,
                        message=f"Registered on {pending.netuid} with UID {uid}",
                        extrinsic_id=pending.tx_id,
                        uid=uid,
                        paid_tao=paid or 0.0,
                    ),
                )
            return True

        elapsed = asyncio.get_running_loop().time() - pending.created_at
        if elapsed < 180:
            return True

        self._pending_registrations.pop(order.id, None)
        await self.orders.set_last_error_if_active(
            order.id,
            "Submitted transaction was not confirmed on-chain; retrying when target is still valid.",
        )
        await self.notifier.send(
            order.chat_id,
            f"⚠️ Registration tx for order <code>{order.id[:8]}</code> was submitted "
            "but is still not confirmed on-chain. I will retry if the price is still at target.",
        )
        return False

    async def _get_balance_cached(self, order: Order) -> float | None:
        params = order.params
        key = (order.user_id, params.coldkey_name, params.hotkey_name)
        loop = asyncio.get_running_loop()
        now = loop.time()
        cached = self._balance_cache.get(key)
        if cached is not None and now - cached[1] < self.settings.price_notify_interval_sec:
            return cached[0]
        balance = await self.chain.get_coldkey_balance_tao(
            params.coldkey_name,
            self._wallet_path(order.user_id),
            params.hotkey_name,
        )
        if balance is not None:
            self._balance_cache[key] = (balance, now)
        return balance

    async def _refresh_price_alert(self, order: Order, current_price: float) -> None:
        """Replace alert when chain price moves — delete old, send new (with notification)."""
        await self._send_price_alert(order, current_price)

    async def _send_price_alert(self, order: Order, current_price: float) -> None:
        if order.id in self._price_notify_suppressed:
            return
        fresh = await self.orders.get(order.id)
        if fresh is None or not fresh.is_active():
            return
        if fresh.id in self._price_notify_suppressed:
            return
        if current_price <= fresh.params.max_price_tao:
            await self._clear_price_alert(fresh)
            return
        if self._registration_key(fresh) in self._registering:
            return

        old_message_id = fresh.price_message_id
        chat_id = fresh.chat_id
        fresh.last_price = current_price
        balance = await self._get_balance_cached(fresh)
        message_id = await self.notifier.replace_price_message(
            chat_id,
            price_update_message(fresh, balance),
            old_message_id=old_message_id,
        )
        if message_id is None:
            await self.orders.touch_price_notify_if_active(fresh.id)
            if self.notifier.chat_in_backoff(chat_id):
                logger.debug(
                    "Price alert deferred (flood control) order=%s chat=%s",
                    fresh.id[:8],
                    chat_id,
                )
            else:
                logger.warning(
                    "Price alert send failed order=%s chat=%s", fresh.id[:8], chat_id
                )
            return

        now = iso(utc_now())
        saved = await self.orders.update_notify_if_active(
            fresh.id,
            price_message_id=message_id,
            last_price_notify_at=now,
        )
        if not saved:
            logger.warning(
                "Could not persist price alert order=%s msg=%s",
                fresh.id[:8],
                message_id,
            )
            await self.notifier.delete_message(chat_id, message_id)
            await self.orders.reset_price_notify_if_active(fresh.id)
            return

        action = "replaced" if old_message_id is not None else "posted"
        logger.debug(
            "Price alert %s order=%s chat=%s msg=%s price=%.6f",
            action,
            fresh.id[:8],
            chat_id,
            message_id,
            current_price,
        )

    def _schedule_price_notify(self, order: Order, current_price: float) -> None:
        if order.id in self._price_notify_inflight:
            return
        self._price_notify_inflight.add(order.id)
        task = asyncio.create_task(
            self._notify_price(order, current_price),
            name=f"price-notify-{order.id[:8]}",
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        task.add_done_callback(
            lambda _t, oid=order.id: self._price_notify_inflight.discard(oid)
        )

    async def _notify_price(self, order: Order, current_price: float) -> None:
        try:
            await self._notify_price_inner(order, current_price)
        except Exception:
            logger.exception("Price notify failed order=%s", order.id[:8])

    async def _notify_price_inner(self, order: Order, current_price: float) -> None:
        await self._send_price_alert(order, current_price)

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
        await self._remove_price_message(order)
        await self.notifier.send(
            order.chat_id,
            f"✅ Order <code>{order.id[:8]}</code>: hotkey <b>{params.hotkey_name}</b> "
            f"is already registered on SN{params.netuid}. Order closed.",
        )
        return True

    def _should_notify_price(self, order: Order) -> bool:
        """Whether a new Telegram price alert must be posted (no visible message yet)."""
        if order.price_message_id is not None:
            return False
        interval = self.settings.price_notify_interval_sec
        if order.last_price_notify_at is None:
            return True
        elapsed = (utc_now() - parse_iso(order.last_price_notify_at)).total_seconds()
        return elapsed >= interval

    def _should_replace_price(self, order: Order) -> bool:
        """Throttle delete+send replaces to avoid Telegram flood control."""
        if order.last_price_notify_at is None:
            return True
        interval = min(self.settings.price_notify_interval_sec, 30)
        elapsed = (utc_now() - parse_iso(order.last_price_notify_at)).total_seconds()
        return elapsed >= interval

    async def _clear_price_alert(self, order: Order) -> None:
        """Delete the Telegram price alert and clear DB tracking."""
        msg_id = order.price_message_id
        chat_id = order.chat_id
        if msg_id is None:
            return
        await self.orders.clear_price_message_if_active(order.id)
        self.notifier.delete_later(chat_id, msg_id)
        order.price_message_id = None

    async def _remove_price_message(self, order: Order) -> None:
        fresh = await self.orders.get(order.id)
        if fresh is None or fresh.price_message_id is None:
            return
        msg_id = fresh.price_message_id
        chat_id = fresh.chat_id
        await self.orders.clear_price_message_if_active(fresh.id)
        self.notifier.delete_later(chat_id, msg_id)

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

    async def _attempt_batch_registration(
        self,
        netuid: int,
        orders: list[Order],
        snapshot: NetuidRegistrationSnapshot,
        target_price: float,
    ) -> None:
        """Register multiple orders with the same price limit simultaneously."""
        logger.info(
            "Batch target hit SN%d orders=%d snapshot_price=%.6f target=%.6f",
            netuid,
            len(orders),
            snapshot.price_tao,
            target_price,
        )

        async def _prepare_one(
            order: Order,
        ) -> tuple[Order, PreparedSubmission | None]:
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
                    self.notifier.send_later(
                        order.chat_id,
                        f"🔒 Batch registration skipped for order <code>{order.id[:8]}</code>: "
                        f"wallet <b>{params.coldkey_name}</b> is locked. Use /unlock.",
                    )
                return order, None

            try:
                async with session.signing(params.coldkey_name):
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
                    self.notifier.send_later(
                        order.chat_id,
                        f"🔒 Cannot sign for batch order <code>{order.id[:8]}</code>: {exc}",
                    )
                return order, None

            self._wallet_lock_notified.discard(notify_key)
            slippage = self.settings.slippage_tolerance
            limit_price = registration_limit_price(
                snapshot.price_tao, min(target_price, params.max_price_tao), slippage
            )
            label = (
                f"order={order.id[:8]} user={order.user_id} "
                f"{params.coldkey_name}/{params.hotkey_name}"
            )
            return order, PreparedSubmission(
                wallet=wallet,
                password=session.get_password(params.coldkey_name),
                limit_price=limit_price,
                hotkey_ss58=get_hotkey_pub_ss58(wallet),
                label=label,
                unlocked=True,
            )

        reg_keys = [self._registration_key(o) for o in orders]
        suppress_ids = [o.id for o in orders]
        for key in reg_keys:
            self._registering.add(key)
        for oid in suppress_ids:
            self._price_notify_suppressed.add(oid)

        try:
            prep = await asyncio.gather(*(_prepare_one(o) for o in orders))
            prepared: list[tuple[Order, PreparedSubmission]] = [
                (order, item) for order, item in prep if item is not None
            ]
            deferred = [order for order, item in prep if item is None]

            if deferred:
                logger.info(
                    "Batch deferred SN%d orders=%d prepared=%d",
                    netuid,
                    len(deferred),
                    len(prepared),
                )

            if not prepared:
                return

            submissions = [item for _, item in prepared]
            composed = await self._compose_registrations(snapshot, submissions)
            results = await self._fire_registrations(snapshot, composed)

            for (order, prep), result in zip(prepared, results):
                enriched = await self._recover_registration_if_submitted(
                    snapshot.netuid, prep.hotkey_ss58, snapshot.price_tao, result
                )
                enriched = await self._enrich_submit_result(
                    snapshot.netuid, prep.hotkey_ss58, enriched
                )
                finished = await self._handle_registration_result(order, enriched)
                if finished:
                    await self._remove_price_message(order)
        finally:
            for key in reg_keys:
                self._registering.discard(key)
            for oid in suppress_ids:
                self._price_notify_suppressed.discard(oid)

    async def _compose_registrations(
        self,
        snapshot: NetuidRegistrationSnapshot,
        prepared: list[PreparedSubmission],
    ) -> list[RegistrationResult | ComposedRegistration]:
        return await self.chain.compose_registration_calls(snapshot, prepared)

    async def _fire_registrations(
        self,
        snapshot: NetuidRegistrationSnapshot,
        composed: list[RegistrationResult | ComposedRegistration],
    ) -> list[RegistrationResult]:
        final: list[RegistrationResult | None] = [None] * len(composed)
        to_fire: list[ComposedRegistration] = []
        fire_indices: list[int] = []

        for idx, entry in enumerate(composed):
            if isinstance(entry, RegistrationResult):
                final[idx] = entry
            else:
                fire_indices.append(idx)
                to_fire.append(entry)

        if to_fire:
            fired = await self.chain.fire_registration_calls(snapshot, to_fire)
            for idx, result in zip(fire_indices, fired):
                final[idx] = result

        return [
            r
            if r is not None
            else RegistrationResult(
                success=False, message="Internal submit error", extrinsic_id=None
            )
            for r in final
        ]

    async def _enrich_submit_result(
        self, netuid: int, hotkey_ss58: str, result
    ):
        if not result.success:
            return result
        uid = await self.chain.lookup_registration_uid(netuid, hotkey_ss58)
        if uid is None:
            return RegistrationResult(
                success=False,
                message=(
                    "Transaction submitted but registration is not confirmed yet. "
                    "I will keep checking on-chain and will not mark it successful "
                    "until the hotkey is registered."
                ),
                extrinsic_id=result.extrinsic_id,
                uid=None,
                paid_tao=result.paid_tao,
                submitted=True,
                hotkey_ss58=hotkey_ss58,
            )
        return RegistrationResult(
            success=True,
            message=f"Registered on {netuid} with UID {uid}",
            extrinsic_id=result.extrinsic_id,
            uid=uid,
            paid_tao=result.paid_tao,
            submitted=result.submitted,
            hotkey_ss58=hotkey_ss58,
        )

    async def _recover_registration_if_submitted(
        self,
        netuid: int,
        hotkey_ss58: str,
        paid_tao: float,
        result: RegistrationResult,
    ) -> RegistrationResult:
        """Detect on-chain success when broadcast succeeded but post-send bookkeeping failed."""
        if result.success:
            return result
        uid = await self.chain.lookup_registration_uid(
            netuid, hotkey_ss58, retries=4, retry_delay_sec=2
        )
        if uid is None and await self.chain.is_hotkey_registered_ss58(netuid, hotkey_ss58):
            uid = await self.chain.lookup_registration_uid(netuid, hotkey_ss58)
        if uid is None:
            return result
        logger.info(
            "Recovered successful registration on SN%d hotkey=%s uid=%s after: %s",
            netuid,
            hotkey_ss58[:8],
            uid,
            result.message,
        )
        return RegistrationResult(
            success=True,
            message=f"Registered on {netuid} with UID {uid}",
            extrinsic_id=result.extrinsic_id,
            uid=uid,
            paid_tao=paid_tao,
            submitted=result.submitted,
            hotkey_ss58=hotkey_ss58,
        )

    async def _handle_registration_result(self, order: Order, result) -> bool:
        """Apply registration outcome to an order. Returns True when monitoring stops."""
        params = order.params
        order = await self.orders.get(order.id) or order
        if not order.is_active():
            return True

        if result.success:
            if result.message == "Already registered":
                if await self.orders.complete_if_active(order.id):
                    await self._remove_price_message(order)
                    await self.notifier.send(
                        order.chat_id,
                        f"✅ Order <code>{order.id[:8]}</code>: hotkey "
                        f"<b>{params.hotkey_name}</b> is already registered on SN{params.netuid}.",
                    )
                return True
            completed = await self.orders.complete_if_active(
                order.id, last_tx_id=result.extrinsic_id
            )
            if not completed:
                return True
            await self._remove_price_message(order)
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
        if result.submitted:
            hotkey_ss58 = result.hotkey_ss58
            if hotkey_ss58 is not None:
                self._pending_registrations[order.id] = PendingRegistration(
                    netuid=params.netuid,
                    hotkey_ss58=hotkey_ss58,
                    tx_id=result.extrinsic_id,
                    paid_tao=result.paid_tao,
                    created_at=asyncio.get_running_loop().time(),
                )
            await self.notifier.send(
                order.chat_id,
                f"⏳ Registration tx submitted for order <code>{order.id[:8]}</code>, "
                "but it is not confirmed on-chain yet.\n"
                f"Tx: <code>{result.extrinsic_id}</code>\n"
                "I will keep checking and will only send success after the hotkey is registered.",
            )
            logger.warning(
                "Registration submitted but unconfirmed order=%s tx=%s",
                order.id[:8],
                result.extrinsic_id,
            )
            return False
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
            notify_key = f"{order.user_id}:{params.coldkey_name}:{order.id}"
            self._wallet_lock_notified.discard(notify_key)
        logger.info("Registration will retry order=%s: %s", order.id[:8], result.message)
        return False

    async def _attempt_registration(
        self,
        order: Order,
        snapshot: NetuidRegistrationSnapshot,
        target_price: float,
    ) -> bool:
        """Returns True when monitoring for this order should stop."""
        params = order.params
        session = self.sessions.for_user(order.user_id)
        wallet_path = self._wallet_path(order.user_id)
        notify_key = f"{order.user_id}:{params.coldkey_name}:{order.id}"
        reg_key = self._registration_key(order)

        self._registering.add(reg_key)
        self._price_notify_suppressed.add(order.id)
        try:
            if not session.has_password(params.coldkey_name):
                await self.orders.set_last_error_if_active(
                    order.id,
                    f"Wallet locked: unlock {params.coldkey_name} with /unlock",
                )
                if notify_key not in self._wallet_lock_notified:
                    self._wallet_lock_notified.add(notify_key)
                    self.notifier.send_later(
                        order.chat_id,
                        f"🔒 Price hit for order <code>{order.id[:8]}</code> but wallet "
                        f"<b>{params.coldkey_name}</b> is locked. Use /unlock {params.coldkey_name}",
                    )
                return False

            try:
                async with session.signing(params.coldkey_name):
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
                    self.notifier.send_later(
                        order.chat_id,
                        f"🔒 Cannot sign for order <code>{order.id[:8]}</code>: {exc}",
                    )
                return False

            self._wallet_lock_notified.discard(notify_key)

            slippage = self.settings.slippage_tolerance
            limit_price = registration_limit_price(
                snapshot.price_tao, target_price, slippage
            )
            hotkey_ss58 = get_hotkey_pub_ss58(wallet)
            prep = PreparedSubmission(
                wallet=wallet,
                password=session.get_password(params.coldkey_name),
                limit_price=limit_price,
                hotkey_ss58=hotkey_ss58,
                label=f"order={order.id[:8]}",
                unlocked=True,
            )

            composed = await self._compose_registrations(snapshot, [prep])
            results = await self._fire_registrations(snapshot, composed)
            result = results[0]
            result = await self._recover_registration_if_submitted(
                params.netuid, hotkey_ss58, snapshot.price_tao, result
            )
            result = await self._enrich_submit_result(
                params.netuid, hotkey_ss58, result
            )
            return await self._handle_registration_result(order, result)
        finally:
            self._registering.discard(reg_key)
            self._price_notify_suppressed.discard(order.id)
