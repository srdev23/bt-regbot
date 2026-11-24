"""Regression: RPC/false-negative subnet_exists must not mass-fail orders."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from sn_reg_bot.chain.service import (
    ChainConnectionError,
    NetuidRegistrationSnapshot,
)
from sn_reg_bot.models.orders import Order, SubnetRegistrationParams
from sn_reg_bot.monitor.coordinator import (
    OrderMonitorCoordinator,
    _SUBNET_MISSING_CONFIRM_TICKS,
)


def _settings(**overrides):
    defaults = {
        "price_notify_interval_sec": 60,
        "monitor_tick_sec": 5,
        "bot_owner_id": None,
        "slippage_tolerance": 0.005,
    }
    defaults.update(overrides)
    return type("S", (), defaults)()


def _order(user_id: int = 1, max_price: float = 0.5) -> Order:
    params = SubnetRegistrationParams(15, "c1", "h1", max_price)
    return Order.create_subnet_registration(
        user_id=user_id, chat_id=100, params=params
    )


def _snapshot(price: float = 0.2) -> NetuidRegistrationSnapshot:
    return NetuidRegistrationSnapshot(
        netuid=15,
        block_hash="0xabc",
        burn=MagicMock(tao=price),
        price_tao=price,
        era_={"period": 10, "current": 1},
    )


def _coordinator(
    *,
    orders: list[Order],
    exists_side_effect,
    snapshot: NetuidRegistrationSnapshot | Exception | None = None,
) -> tuple[OrderMonitorCoordinator, AsyncMock]:
    order_repo = AsyncMock()
    order_repo.list_active = AsyncMock(return_value=orders)
    order_repo.fail_if_active = AsyncMock(return_value=True)
    order_repo.get = AsyncMock(side_effect=lambda oid: next(o for o in orders if o.id == oid))
    order_repo.clear_price_message_if_active = AsyncMock(return_value=True)
    order_repo.update_price_if_active = AsyncMock(return_value=True)

    users = AsyncMock()
    users.is_approved = AsyncMock(return_value=True)
    users.get = AsyncMock(return_value=None)

    chain = AsyncMock()
    chain.subnet_exists = AsyncMock(side_effect=exists_side_effect)
    if isinstance(snapshot, Exception):
        chain.capture_registration_snapshot = AsyncMock(side_effect=snapshot)
    else:
        chain.capture_registration_snapshot = AsyncMock(
            return_value=snapshot if snapshot is not None else _snapshot()
        )

    notifier = AsyncMock()
    owner_alerts = AsyncMock()
    owner_alerts.send = AsyncMock()

    coord = OrderMonitorCoordinator(
        settings=_settings(),
        orders=order_repo,
        users=users,
        chain=chain,
        sessions=MagicMock(),
        price_cache=MagicMock(),
        notifier=notifier,
        owner_alerts=owner_alerts,
    )
    # Avoid real registration path in process tests that reach it
    coord._process_netuid_orders = AsyncMock()
    return coord, order_repo


@pytest.mark.asyncio
async def test_rpc_exception_does_not_fail_orders():
    order = _order()
    coord, order_repo = _coordinator(
        orders=[order],
        exists_side_effect=RuntimeError("websocket closed"),
    )

    await coord._tick_all()

    order_repo.fail_if_active.assert_not_called()
    coord._process_netuid_orders.assert_not_called()


@pytest.mark.asyncio
async def test_chain_connection_error_propagates():
    order = _order()
    coord, order_repo = _coordinator(
        orders=[order],
        exists_side_effect=ChainConnectionError("503"),
    )

    with pytest.raises(ChainConnectionError):
        await coord._tick_all()

    order_repo.fail_if_active.assert_not_called()


@pytest.mark.asyncio
async def test_single_false_does_not_fail_orders():
    order = _order()
    coord, order_repo = _coordinator(
        orders=[order],
        exists_side_effect=[False],
    )

    await coord._tick_all()

    order_repo.fail_if_active.assert_not_called()
    assert coord._subnet_missing_ticks[15] == 1


@pytest.mark.asyncio
async def test_sustained_false_fails_orders():
    order = _order()
    coord, order_repo = _coordinator(
        orders=[order],
        exists_side_effect=[False] * _SUBNET_MISSING_CONFIRM_TICKS,
    )

    for _ in range(_SUBNET_MISSING_CONFIRM_TICKS):
        await coord._tick_all()

    order_repo.fail_if_active.assert_called_once()
    assert order_repo.fail_if_active.call_args.args[0] == order.id
    assert "does not exist" in order_repo.fail_if_active.call_args.args[1]


@pytest.mark.asyncio
async def test_true_after_miss_resets_counter():
    order = _order()
    coord, order_repo = _coordinator(
        orders=[order],
        exists_side_effect=[False, True],
        snapshot=_snapshot(0.25),
    )

    await coord._tick_all()
    await coord._tick_all()

    order_repo.fail_if_active.assert_not_called()
    assert 15 not in coord._subnet_missing_ticks
    coord._process_netuid_orders.assert_called_once()


@pytest.mark.asyncio
async def test_zero_price_snapshot_skips_without_failing():
    order = _order()
    coord, order_repo = _coordinator(
        orders=[order],
        exists_side_effect=[True],
        snapshot=_snapshot(0.0),
    )

    await coord._tick_all()

    order_repo.fail_if_active.assert_not_called()
    coord._process_netuid_orders.assert_not_called()
