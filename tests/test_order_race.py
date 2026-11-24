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
async def test_update_max_price_if_active_does_not_revive_completed(repo: OrderRepository):
    params = SubnetRegistrationParams(15, "ckey", "hkey", 0.5)
    order = Order.create_subnet_registration(user_id=1, chat_id=100, params=params)
    await repo.save(order)
    assert await repo.complete_if_active(order.id, last_tx_id="0xabc")

    updated = await repo.update_max_price_if_active(1, order.id, 0.25)
    assert updated is None

    loaded = await repo.get(order.id)
    assert loaded is not None
    assert loaded.status == OrderStatus.COMPLETED
    assert loaded.params.max_price_tao == 0.5


@pytest.mark.asyncio
async def test_complete_if_active_is_idempotent(repo: OrderRepository):
    params = SubnetRegistrationParams(15, "ckey", "hkey", 0.5)
    order = Order.create_subnet_registration(user_id=1, chat_id=100, params=params)
    await repo.save(order)

    assert await repo.complete_if_active(order.id)
    assert not await repo.complete_if_active(order.id)

    loaded = await repo.get(order.id)
    assert loaded is not None
    assert loaded.status == OrderStatus.COMPLETED


@pytest.mark.asyncio
async def test_update_max_price_only_changes_params(repo: OrderRepository):
    params = SubnetRegistrationParams(15, "ckey", "hkey", 0.5)
    order = Order.create_subnet_registration(user_id=1, chat_id=100, params=params)
    await repo.save(order)
    await repo.complete_if_active(order.id)

    stale = await repo.get(order.id)
    assert stale is not None
    stale.status = OrderStatus.ACTIVE
    stale.params.max_price_tao = 0.1
    await repo.save(stale)

    loaded = await repo.get(order.id)
    assert loaded is not None
    assert loaded.status == OrderStatus.ACTIVE
    assert loaded.params.max_price_tao == 0.1

    assert await repo.complete_if_active(order.id)
    fixed = await repo.get(order.id)
    assert fixed is not None
    assert fixed.status == OrderStatus.COMPLETED
