from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from sn_reg_bot.chain.service import (
    NetuidRegistrationSnapshot,
    RegistrationResult,
)
from sn_reg_bot.models.orders import Order, SubnetRegistrationParams, iso, utc_now
from sn_reg_bot.monitor.coordinator import OrderMonitorCoordinator


def _coordinator(**overrides) -> OrderMonitorCoordinator:
    defaults = dict(
        settings=type(
            "S",
            (),
            {
                "price_notify_interval_sec": 60,
                "monitor_tick_sec": 5,
                "bot_owner_id": None,
            },
        )(),
        orders=None,
        users=None,
        chain=None,
        sessions=None,
        price_cache=None,
        notifier=None,
        owner_alerts=None,
    )
    defaults.update(overrides)
    return OrderMonitorCoordinator(**defaults)


def _order(**kwargs) -> Order:
    params = SubnetRegistrationParams(15, "c1", "h1", 0.5)
    order = Order.create_subnet_registration(user_id=1, chat_id=100, params=params)
    for key, value in kwargs.items():
        setattr(order, key, value)
    return order


def test_should_notify_immediately_when_no_message_id():
    c = _coordinator()
    order = _order(price_message_id=None, last_price_notify_at=None)
    assert c._should_notify_price(order) is True


def test_should_retry_soon_when_message_id_lost_but_recent_notify():
    c = _coordinator()
    recent = iso(utc_now() - timedelta(seconds=5))
    order = _order(price_message_id=None, last_price_notify_at=recent)
    assert c._should_notify_price(order) is False
    older = iso(utc_now() - timedelta(seconds=15))
    order = _order(price_message_id=None, last_price_notify_at=older)
    assert c._should_notify_price(order) is True


def test_should_not_repost_when_message_already_visible():
    c = _coordinator()
    order = _order(price_message_id=12345, last_price_notify_at=None)
    assert c._should_notify_price(order) is False


def test_price_changed_detects_moves():
    c = _coordinator()
    assert c._price_changed(0.5, 0.5) is False
    assert c._price_changed(0.5, 0.501) is True
    assert c._price_changed(None, 0.5) is True


@pytest.mark.asyncio
async def test_submitted_without_uid_is_not_success():
    class Chain:
        async def lookup_registration_uid(self, *_args, **_kwargs):
            return None

    c = _coordinator(chain=Chain())

    result = await c._enrich_submit_result(
        15,
        "hotkey_ss58",
        RegistrationResult(
            success=True,
            message="Submitted",
            extrinsic_id="0xabc",
            paid_tao=0.5,
            submitted=True,
            hotkey_ss58="hotkey_ss58",
        ),
    )

    assert result.success is False
    assert result.submitted is True
    assert result.extrinsic_id == "0xabc"


@pytest.mark.asyncio
async def test_handle_submitted_unconfirmed_keeps_order_active():
    order = _order()

    class Orders:
        def __init__(self):
            self.last_error = None
            self.completed = False

        async def get(self, _order_id):
            return order

        async def set_last_error_if_active(self, _order_id, error):
            self.last_error = error
            return True

        async def complete_if_active(self, *_args, **_kwargs):
            self.completed = True
            return True

    notifier = type("Notifier", (), {"send": AsyncMock(return_value=True)})()
    orders = Orders()
    c = _coordinator(orders=orders, notifier=notifier)

    finished = await c._handle_registration_result(
        order,
        RegistrationResult(
            success=False,
            message="Transaction submitted but registration is not confirmed yet.",
            extrinsic_id="0xabc",
            paid_tao=0.5,
            submitted=True,
            hotkey_ss58="hotkey_ss58",
        ),
    )

    assert finished is False
    assert order.id in c._pending_registrations
    assert orders.completed is False
    notifier.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_batch_limit_price_never_exceeds_exact_order_target(monkeypatch):
    order = _order()
    order.params.max_price_tao = 0.5000006

    class Session:
        def has_password(self, _coldkey):
            return True

        def get_password(self, _coldkey):
            return "pw"

        def signing(self, _coldkey):
            class Ctx:
                async def __aenter__(self):
                    return None

                async def __aexit__(self, *_args):
                    return None

            return Ctx()

        def prepare_for_signing(self, *_args):
            wallet = MagicMock()
            wallet.coldkeypub.ss58_address = "cold"
            return wallet

    class Sessions:
        def for_user(self, _user_id):
            return Session()

    class Orders:
        async def set_last_error_if_active(self, *_args):
            return True

    settings = type(
        "S",
        (),
        {
            "price_notify_interval_sec": 60,
            "monitor_tick_sec": 5,
            "bot_owner_id": None,
            "slippage_tolerance": 1.0,
            "user_wallet_path": lambda self, _uid: "/tmp",
        },
    )()
    notifier = type("Notifier", (), {"send_later": lambda *_args, **_kwargs: None})()
    c = _coordinator(
        settings=settings,
        orders=Orders(),
        sessions=Sessions(),
        notifier=notifier,
    )
    captured_limits: list[float] = []

    async def compose(_snapshot, prepared):
        captured_limits.extend(item.limit_price for item in prepared)
        return []

    async def fire(_snapshot, _composed):
        return []

    monkeypatch.setattr(
        "sn_reg_bot.monitor.coordinator.get_hotkey_pub_ss58",
        lambda _wallet: "hotkey_ss58",
    )
    c._compose_registrations = compose
    c._fire_registrations = fire
    snapshot = NetuidRegistrationSnapshot(
        netuid=15,
        block_hash="0xblock",
        burn=MagicMock(tao=0.49),
        price_tao=0.49,
        era_={"period": 1, "current": 1},
    )

    await c._attempt_batch_registration(
        15,
        [order],
        snapshot,
        target_price=0.500001,
    )

    assert captured_limits == [order.params.max_price_tao]
