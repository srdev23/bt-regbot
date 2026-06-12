from __future__ import annotations

import asyncio
import logging

from regbot.chain import ChainService
from regbot.formatting import (
    hotkey_already_registered_message,
    order_completed_message,
    order_expired_message,
    order_failed_message,
    price_hit_message,
    price_update_message,
    registration_failed_message,
    registration_success_message,
    wallet_locked_message,
)
from regbot.models import OrderStatus, RegistrationOrder, iso, parse_iso, utc_now
from regbot.notifier import TelegramNotifier
from regbot.order_store import OrderStore
from regbot.wallet_session import WalletSessionManager

logger = logging.getLogger(__name__)


class OrderMonitor:
    def __init__(
        self,
        *,
        store: OrderStore,
        chain: ChainService,
        wallets: WalletSessionManager,
        wallet_path: str,
        slippage: float,
        price_notify_interval_sec: int,
        notifier: TelegramNotifier,
    ) -> None:
        self.store = store
        self.chain = chain
        self.wallets = wallets
        self.wallet_path = wallet_path
        self.slippage = slippage
        self.price_notify_interval_sec = price_notify_interval_sec
        self.notifier = notifier
        self._tasks: dict[str, asyncio.Task] = {}
        self._registering: set[str] = set()
        self._wallet_lock_notified: set[str] = set()
        self._lock = asyncio.Lock()

    def clear_wallet_lock_notified(self, wallet_name: str) -> None:
        for order in self.store.list_active():
            if order.wallet_name == wallet_name:
                self._wallet_lock_notified.discard(order.id)

    @property
    def active_count(self) -> int:
        return len(self._tasks)

    async def start(self) -> None:
        for order in self.store.list_active():
            await self.track(order.id)

    async def stop(self) -> None:
        for task in list(self._tasks.values()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    async def track(self, order_id: str) -> None:
        async with self._lock:
            if order_id in self._tasks:
                return
            self._tasks[order_id] = asyncio.create_task(
                self._run_order(order_id), name=f"order-{order_id[:8]}"
            )

    async def untrack(self, order_id: str) -> None:
        async with self._lock:
            task = self._tasks.pop(order_id, None)
        self._wallet_lock_notified.discard(order_id)
        order = self.store.load(order_id)
        if order:
            await self._remove_price_message(order)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _remove_price_message(self, order: RegistrationOrder) -> None:
        if order.price_message_id is None:
            return
        await self.notifier.delete_message(order.chat_id, order.price_message_id)
        order.price_message_id = None

    async def _finish_order(
        self,
        order: RegistrationOrder,
        status: OrderStatus,
        message: str,
        *,
        remove_price: bool = True,
    ) -> None:
        order.status = status
        if remove_price:
            await self._remove_price_message(order)
        self.store.save(order)
        await self.notifier.send(order.chat_id, message)

    async def _run_order(self, order_id: str) -> None:
        try:
            while True:
                order = self.store.load(order_id)
                if order is None or not order.is_active():
                    return

                if order.is_expired():
                    await self._finish_order(
                        order, OrderStatus.EXPIRED, order_expired_message(order)
                    )
                    return

                try:
                    if await self._tick(order):
                        return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Monitor tick failed for order %s", order_id)
                    order = self.store.load(order_id)
                    if order:
                        order.last_error = str(exc)
                        self.store.save(order)

                order = self.store.load(order_id)
                if order is None or not order.is_active():
                    return
                await asyncio.sleep(order.poll_interval_sec)
        except asyncio.CancelledError:
            logger.info("Stopped monitoring order %s", order_id)

    def _notify_phase_sec(self, order_id: str) -> int:
        """Stagger orders in the same chat across the notify window."""
        spread = max(1, min(30, self.price_notify_interval_sec // 2))
        return int(order_id[:8], 16) % spread

    def _should_notify_price(self, order: RegistrationOrder) -> bool:
        if order.last_price_notify_at is None:
            created_elapsed = (utc_now() - parse_iso(order.created_at)).total_seconds()
            return created_elapsed >= self._notify_phase_sec(order.id)
        elapsed = (utc_now() - parse_iso(order.last_price_notify_at)).total_seconds()
        return elapsed >= self.price_notify_interval_sec

    async def _notify_price(self, order: RegistrationOrder) -> None:
        balance = await self.chain.get_coldkey_balance_tao(
            order.wallet_name, self.wallet_path, order.hotkey
        )
        message_id = await self.notifier.refresh_price_message(
            order.chat_id,
            price_update_message(order, order.remaining_sec(), balance),
            old_message_id=order.price_message_id,
        )
        if message_id is None:
            return
        order.price_message_id = message_id
        order.last_price_notify_at = iso(utc_now())

    async def _tick(self, order: RegistrationOrder) -> bool:
        """Run one monitor cycle. Returns True when monitoring should stop."""
        if not await self.chain.subnet_exists(order.netuid):
            order.last_error = f"Subnet {order.netuid} does not exist"
            await self._finish_order(
                order,
                OrderStatus.FAILED,
                order_failed_message(order, f"Subnet SN{order.netuid} not found."),
            )
            return True

        price = await self.chain.get_registration_price(order.netuid)
        order.last_price = price.burn_tao
        order.last_price_at = iso(utc_now())

        if self._should_notify_price(order):
            await self._notify_price(order)

        self.store.save(order)

        if order.registrations_done >= order.reg_n:
            await self._finish_order(
                order, OrderStatus.COMPLETED, order_completed_message(order)
            )
            return True

        if price.burn_tao > order.reg_at:
            return False

        if order.id in self._registering:
            logger.debug(
                "Registration in progress order=%s wallet=%s/%s",
                order.id[:8],
                order.wallet_name,
                order.hotkey,
            )
            return False

        logger.info(
            "Target hit order=%s SN%d wallet=%s/%s price=%.6f target=%.6f",
            order.id[:8],
            order.netuid,
            order.wallet_name,
            order.hotkey,
            price.burn_tao,
            order.reg_at,
        )
        return await self._attempt_registration(order, price.burn_tao)

    async def _notify_wallet_locked(
        self, order: RegistrationOrder, price_tao: float, detail: str
    ) -> None:
        order.last_error = detail
        self.store.save(order)
        if order.id not in self._wallet_lock_notified:
            self._wallet_lock_notified.add(order.id)
            await self.notifier.send(
                order.chat_id,
                wallet_locked_message(order, price_tao),
            )
        logger.warning(
            "Wallet not ready order=%s wallet=%s/%s: %s",
            order.id[:8],
            order.wallet_name,
            order.hotkey,
            detail,
        )

    async def _attempt_registration(
        self, order: RegistrationOrder, price_tao: float
    ) -> bool:
        """Try to register when price target is hit. Returns True if order finished."""
        if not self.wallets.has_password(order.wallet_name):
            await self._notify_wallet_locked(
                order,
                price_tao,
                f"Wallet locked: unlock {order.wallet_name} with /unlock",
            )
            return False

        async with self.wallets.signing(order.wallet_name):
            try:
                wallet = await asyncio.to_thread(
                    self.wallets.prepare_for_signing,
                    order.wallet_name,
                    order.hotkey,
                    self.wallet_path,
                )
            except Exception as exc:
                await self._notify_wallet_locked(order, price_tao, str(exc))
                return False

            self._wallet_lock_notified.discard(order.id)

            try:
                if await self.chain.is_hotkey_registered(order.netuid, wallet):
                    order.registrations_done = order.reg_n
                    order.last_error = None
                    await self._finish_order(
                        order,
                        OrderStatus.COMPLETED,
                        hotkey_already_registered_message(order),
                    )
                    return True

                fresh_price = await self.chain.get_registration_price(order.netuid)
                if fresh_price.burn_tao > order.reg_at:
                    logger.info(
                        "Price rose before submit order=%s price=%.6f target=%.6f",
                        order.id[:8],
                        fresh_price.burn_tao,
                        order.reg_at,
                    )
                    return False

                await self.notifier.send(
                    order.chat_id,
                    price_hit_message(order, fresh_price.burn_tao, self.slippage * 100),
                )

                self._registering.add(order.id)
                try:
                    result = await self.chain.register(
                        wallet,
                        order.netuid,
                        slippage=self.slippage,
                        password=self.wallets.get_password(order.wallet_name),
                        target_price=order.reg_at,
                    )
                finally:
                    self._registering.discard(order.id)

            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "Registration crashed order=%s wallet=%s/%s",
                    order.id,
                    order.wallet_name,
                    order.hotkey,
                )
                order.last_error = str(exc)
                self.store.save(order)
                await self.notifier.send(
                    order.chat_id,
                    registration_failed_message(order, str(exc)),
                )
                return False

        order = self.store.load(order.id) or order
        if result.success:
            order.registrations_done += 1
            order.last_tx_id = result.extrinsic_id
            order.last_error = None
            await self._finish_order(
                order,
                OrderStatus.COMPLETED,
                registration_success_message(
                    order,
                    uid=result.uid,
                    paid_tao=result.paid_tao,
                    extrinsic_id=result.extrinsic_id,
                    message=result.message,
                ),
            )
            return True

        order.last_error = result.message
        self.store.save(order)
        retryable = any(
            token in result.message.lower()
            for token in (
                "invalid",
                "password",
                "unlock",
                "insufficient",
                "price moved",
            )
        )
        if "password" in result.message.lower() or "invalid" in result.message.lower():
            self._wallet_lock_notified.discard(order.id)
        await self.notifier.send(
            order.chat_id,
            registration_failed_message(order, result.message),
        )
        if retryable:
            logger.info(
                "Registration will retry order=%s: %s", order.id[:8], result.message
            )
        return False
