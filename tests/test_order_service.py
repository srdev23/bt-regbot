import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from sn_reg_bot.database.connection import Database
from sn_reg_bot.database.repository import OrderRepository, UserRepository
from sn_reg_bot.models.orders import OrderStatus, SubnetRegistrationParams
from sn_reg_bot.models.orders import Order
from sn_reg_bot.services.order_service import OrderNotEditableError, OrderService
from sn_reg_bot.services.wallet_service import WalletService


class _FakeMonitor:
    async def track(self, order_id: str) -> None:
        pass

    async def untrack(self, order_id: str) -> None:
        pass


class _FakeChain:
    pass


@pytest_asyncio.fixture
async def order_service_setup():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        await db.connect()
        orders = OrderRepository(db)
        users = UserRepository(db)
        ws = WalletService(
            settings=None,  # type: ignore[arg-type]
            chain=_FakeChain(),  # type: ignore[arg-type]
            sessions=None,  # type: ignore[arg-type]
            users=users,
            orders=orders,
        )
        ws.ensure_user = AsyncMock()
        os = OrderService(orders, users, ws, _FakeMonitor())  # type: ignore[arg-type]
        yield os, orders
        await db.close()


@pytest.mark.asyncio
async def test_update_max_price_active_order(order_service_setup):
    os, orders = order_service_setup
    params = SubnetRegistrationParams(15, "ckey", "hkey", 0.5)
    order = Order.create_subnet_registration(user_id=1, chat_id=100, params=params)
    await orders.save(order)

    updated = await os.update_max_price(1, order.id, 0.25)
    assert updated.params.max_price_tao == 0.25

    loaded = await orders.get(order.id)
    assert loaded is not None
    assert loaded.params.max_price_tao == 0.25


@pytest.mark.asyncio
async def test_update_max_price_rejects_non_active(order_service_setup):
    os, orders = order_service_setup
    params = SubnetRegistrationParams(15, "ckey", "hkey", 0.5)
    order = Order.create_subnet_registration(user_id=1, chat_id=100, params=params)
    await orders.save(order)
    await orders.cancel(1, order.id)

    with pytest.raises(OrderNotEditableError):
        await os.update_max_price(1, order.id, 0.25)


@pytest.mark.asyncio
async def test_update_max_price_rejects_invalid_price(order_service_setup):
    os, orders = order_service_setup
    params = SubnetRegistrationParams(15, "ckey", "hkey", 0.5)
    order = Order.create_subnet_registration(user_id=1, chat_id=100, params=params)
    await orders.save(order)

    with pytest.raises(ValueError):
        await os.update_max_price(1, order.id, 0)


@pytest.mark.asyncio
async def test_update_max_price_user_isolation(order_service_setup):
    os, orders = order_service_setup
    params = SubnetRegistrationParams(15, "ckey", "hkey", 0.5)
    order = Order.create_subnet_registration(user_id=1, chat_id=100, params=params)
    await orders.save(order)

    with pytest.raises(OrderNotEditableError):
        await os.update_max_price(2, order.id, 0.25)
