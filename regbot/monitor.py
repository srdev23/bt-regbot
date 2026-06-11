from __future__ import annotations

import asyncio
import logging

from regbot.chain import ChainService
from regbot.formatting import (
    order_completed_message,
    order_expired_message,
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
        await self.notifier.clear_price_panel(order_id)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _run_order(self, order_id: str) -> None:
        try:
            while True:
                order = self.store.load(order_id)
                if order is None or order.status != OrderStatus.ACTIVE:
                    return

                if order.is_expired():
                    order.status = OrderStatus.EXPIRED
                    self.store.save(order)
                    await self.notifier.clear_price_panel(order.id)
                    await self.notifier.send(
                        order.chat_id,
                        order_expired_message(order),
                    )
                    return

                try:
                    await self._tick(order)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception("Monitor tick failed for order %s", order_id)
                    order = self.store.load(order_id)
                    if order:
                        order.last_error = str(exc)
                        self.store.save(order)

                order = self.store.load(order_id)
                if order is None or order.status != OrderStatus.ACTIVE:
                    return
                await asyncio.sleep(order.poll_interval_sec)
        except asyncio.CancelledError:
            logger.info("Stopped monitoring order %s", order_id)

    async def _tick(self, order: RegistrationOrder) -> None:
        if not await self.chain.subnet_exists(order.netuid):
            order.status = OrderStatus.FAILED
            order.last_error = f"Subnet {order.netuid} does not exist"
            self.store.save(order)
            await self.notifier.clear_price_panel(order.id)
            await self.notifier.send(
                order.chat_id,
                f"❌ <b>Order failed</b>\nSubnet SN{order.netuid} not found.",
            )
            return

        price = await self.chain.get_registration_price(order.netuid)
        order.last_price = price.burn_tao
        order.last_price_at = iso(utc_now())

        should_notify_price = False
        if order.last_price_notify_at is None:
            should_notify_price = True
        else:
            elapsed = (utc_now() - parse_iso(order.last_price_notify_at)).total_seconds()
            should_notify_price = elapsed >= self.price_notify_interval_sec

        if should_notify_price:
            order.last_price_notify_at = iso(utc_now())
            balance = await self.chain.get_coldkey_balance_tao(
                order.wallet_name, self.wallet_path, order.hotkey
            )
            await self.notifier.update_price_panel(
                order.id,
                order.chat_id,
                price_update_message(order, order.remaining_sec(), balance),
                lifetime_sec=self.price_notify_interval_sec,
            )

        self.store.save(order)

        if order.registrations_done >= order.reg_n:
            order.status = OrderStatus.COMPLETED
            self.store.save(order)
            await self.notifier.clear_price_panel(order.id)
            await self.notifier.send(order.chat_id, order_completed_message(order))
            return

        if price.burn_tao > order.reg_at:
            return

        if order.id in self._registering:
            return

        wallet = self.wallets.get_wallet(order.wallet_name, order.hotkey, self.wallet_path)
        if wallet is None:
            if order.id not in self._wallet_lock_notified:
                self._wallet_lock_notified.add(order.id)
                order.last_error = (
                    f"Wallet locked: unlock {order.wallet_name} with /unlock"
                )
                self.store.save(order)
                await self.notifier.send(
                    order.chat_id,
                    wallet_locked_message(order, price.burn_tao),
                )
            return

        if await self.chain.is_hotkey_registered(order.netuid, wallet):
            order.registrations_done = order.reg_n
            order.status = OrderStatus.COMPLETED
            order.last_error = None
            self.store.save(order)
            await self.notifier.clear_price_panel(order.id)
            await self.notifier.send(
                order.chat_id,
                f"✅ Hotkey <code>{order.hotkey}</code> already registered on "
                f"SN{order.netuid}.\n"
                f"Order <code>{order.id[:8]}</code> marked complete.",
            )
            return

        await self.notifier.send(
            order.chat_id,
            price_hit_message(order, price.burn_tao, self.slippage * 100),
        )

        self._registering.add(order.id)
        try:
            result = await self.chain.register(wallet, order.netuid, slippage=self.slippage)
        finally:
            self._registering.discard(order.id)

        order = self.store.load(order.id) or order
        if result.success:
            order.registrations_done += 1
            order.last_tx_id = result.extrinsic_id
            order.last_error = None
            if order.registrations_done >= order.reg_n:
                order.status = OrderStatus.COMPLETED
            self.store.save(order)
            await self.notifier.clear_price_panel(order.id)
            await self.notifier.send(
                order.chat_id,
                registration_success_message(
                    order,
                    uid=result.uid,
                    paid_tao=result.paid_tao,
                    extrinsic_id=result.extrinsic_id,
                    message=result.message,
                ),
            )
        else:
            order.last_error = result.message
            self.store.save(order)
            await self.notifier.send(
                order.chat_id,
                registration_failed_message(order, result.message),
            )
