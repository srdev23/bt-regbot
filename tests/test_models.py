import pytest

from sn_reg_bot.models.errors import FailureKind, classify_registration_failure
from sn_reg_bot.models.orders import (
    Order,
    OrderStatus,
    OrderType,
    SubnetRegistrationParams,
)


def test_subnet_registration_params_roundtrip():
    params = SubnetRegistrationParams(
        netuid=15,
        coldkey_name="ckey01",
        hotkey_name="hkey1",
        max_price_tao=0.1,
    )
    data = params.to_dict()
    restored = SubnetRegistrationParams.from_dict(data)
    assert restored == params


def test_order_create_subnet_registration():
    params = SubnetRegistrationParams(15, "ckey01", "hkey1", 0.1)
    order = Order.create_subnet_registration(
        user_id=123,
        chat_id=456,
        params=params,
        poll_interval_sec=5,
    )
    assert order.order_type == OrderType.SUBNET_REGISTRATION
    assert order.status == OrderStatus.ACTIVE
    assert order.is_active()
    assert order.params.max_price_tao == 0.1


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Insufficient balance: 0.01 TAO", FailureKind.RETRYABLE),
        ("Price moved above target", FailureKind.RETRYABLE),
        ("Subnet 999 does not exist", FailureKind.UNRECOVERABLE),
        ("Cannot read wallet keyfile", FailureKind.UNRECOVERABLE),
        ("Invalid password", FailureKind.RETRYABLE),
        ("Chain connection lost", FailureKind.RETRYABLE),
    ],
)
def test_classify_registration_failure(message, expected):
    assert classify_registration_failure(message) == expected
