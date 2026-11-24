import json
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from sn_reg_bot.database.connection import Database
from sn_reg_bot.database.repository import OrderRepository
from sn_reg_bot.models.orders import Order, OrderStatus, SubnetRegistrationParams


@pytest_asyncio.fixture
async def repo():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        await db.connect()
        orders = OrderRepository(db)
        yield orders
        await db.close()


@pytest.mark.asyncio
async def test_save_and_load_order(repo: OrderRepository):
    params = SubnetRegistrationParams(15, "ckey", "hkey", 0.5)
    order = Order.create_subnet_registration(
        user_id=1, chat_id=100, params=params, poll_interval_sec=5
    )
    await repo.save(order)
    loaded = await repo.get(order.id)
    assert loaded is not None
    assert loaded.params.netuid == 15
    assert loaded.params.coldkey_name == "ckey"


@pytest.mark.asyncio
async def test_duplicate_active_order(repo: OrderRepository):
    params = SubnetRegistrationParams(15, "ckey", "hkey", 0.5)
    order1 = Order.create_subnet_registration(
        user_id=1, chat_id=100, params=params, poll_interval_sec=5
    )
    order2 = Order.create_subnet_registration(
        user_id=1, chat_id=100, params=params, poll_interval_sec=5
    )
    await repo.save(order1)
    assert await repo.has_active_duplicate(order2)


@pytest.mark.asyncio
async def test_cancel_order(repo: OrderRepository):
    params = SubnetRegistrationParams(15, "ckey", "hkey", 0.5)
    order = Order.create_subnet_registration(
        user_id=1, chat_id=100, params=params, poll_interval_sec=5
    )
    await repo.save(order)
    cancelled = await repo.cancel(1, order.id)
    assert cancelled is not None
    assert cancelled.status == OrderStatus.CANCELLED


@pytest.mark.asyncio
async def test_user_isolation(repo: OrderRepository):
    params = SubnetRegistrationParams(15, "ckey", "hkey", 0.5)
    order = Order.create_subnet_registration(
        user_id=1, chat_id=100, params=params, poll_interval_sec=5
    )
    await repo.save(order)
    assert await repo.get_for_user(2, order.id) is None
