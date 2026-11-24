from __future__ import annotations

import logging

from sn_reg_bot.database.repository import OrderRepository, UserRepository
from sn_reg_bot.models.orders import Order, OrderStatus, SubnetRegistrationParams
from sn_reg_bot.monitor.coordinator import OrderMonitorCoordinator
from sn_reg_bot.services.wallet_service import WalletService

logger = logging.getLogger(__name__)


class DuplicateActiveOrderError(Exception):
    pass


class OrderNotEditableError(Exception):
    pass


class OrderService:
    def __init__(
        self,
        orders: OrderRepository,
        users: UserRepository,
        wallet_service: WalletService,
        monitor: OrderMonitorCoordinator,
    ) -> None:
        self.orders = orders
        self.users = users
        self.wallet_service = wallet_service
        self.monitor = monitor

    async def create_subnet_registration_order(
        self,
        *,
        user_id: int,
        chat_id: int,
        netuid: int,
        coldkey_name: str,
        hotkey_name: str,
        max_price_tao: float,
    ) -> Order:
        await self.wallet_service.ensure_user(user_id)
        self.wallet_service.validate_wallet_ownership(user_id, coldkey_name)
        manager = self.wallet_service.manager_for(user_id)
        if not manager.hotkey_exists(coldkey_name, hotkey_name):
            raise ValueError(
                f"Hotkey '{hotkey_name}' not found on coldkey '{coldkey_name}'."
            )

        params = SubnetRegistrationParams(
            netuid=netuid,
            coldkey_name=coldkey_name,
            hotkey_name=hotkey_name,
            max_price_tao=max_price_tao,
        )
        order = Order.create_subnet_registration(
            user_id=user_id,
            chat_id=chat_id,
            params=params,
            poll_interval_sec=0,
        )

        if await self.orders.has_active_duplicate(order):
            raise DuplicateActiveOrderError(
                f"Active order already exists for {coldkey_name}/{hotkey_name} on SN{netuid}."
            )

        await self.orders.save(order)
        await self.monitor.track(order.id)
        logger.info(
            "Created order %s user=%s SN%d %s/%s target=%.6f",
            order.id[:8],
            user_id,
            netuid,
            coldkey_name,
            hotkey_name,
            max_price_tao,
        )
        return order

    async def list_orders(self, user_id: int, *, limit: int = 15) -> list[Order]:
        return await self.orders.list_for_user(user_id, limit=limit)

    async def list_active_orders(self, user_id: int) -> list[Order]:
        return await self.orders.list_for_user(user_id, active_only=True, limit=100)

    async def get_order(self, user_id: int, order_id: str) -> Order | None:
        return await self.orders.get_for_user(user_id, order_id)

    async def cancel_order(self, user_id: int, order_id: str) -> Order | None:
        order = await self.orders.cancel(user_id, order_id)
        if order is not None:
            await self.monitor.untrack(order_id)
        return order

    async def update_max_price(
        self,
        user_id: int,
        order_id: str,
        max_price_tao: float,
    ) -> Order:
        if max_price_tao <= 0:
            raise ValueError("Max price must be a positive number.")

        order = await self.orders.get_for_user(user_id, order_id)
        if order is None:
            raise OrderNotEditableError("Order not found.")
        if not order.is_active():
            raise OrderNotEditableError(
                f"Order is {order.status.value}; only active orders can be edited."
            )

        old_price = order.params.max_price_tao
        updated = await self.orders.update_max_price_if_active(
            user_id, order_id, max_price_tao
        )
        if updated is None:
            raise OrderNotEditableError(
                "Order not found or no longer active; could not update price."
            )
        logger.info(
            "Updated order %s user=%s max_price %.6f -> %.6f",
            updated.id[:8],
            user_id,
            old_price,
            max_price_tao,
        )
        return updated
