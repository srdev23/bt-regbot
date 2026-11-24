import pytest

from sn_reg_bot.chain.service import registration_limit_price
from sn_reg_bot.models.orders import Order, SubnetRegistrationParams
from sn_reg_bot.monitor.batch import group_orders_for_batch, price_bucket


def _order(max_price: float, coldkey: str = "c1", hotkey: str = "h1") -> Order:
    params = SubnetRegistrationParams(15, coldkey, hotkey, max_price)
    return Order.create_subnet_registration(user_id=1, chat_id=100, params=params)


def test_price_bucket_rounds_to_six_decimals():
    assert price_bucket(0.5000000004) == 0.5
    assert price_bucket(0.5000004) == 0.500000


def test_group_orders_by_same_max_price():
    o1 = _order(0.5, "c1", "h1")
    o2 = _order(0.5, "c2", "h2")
    o3 = _order(0.25, "c3", "h3")

    groups = group_orders_for_batch([o1, o2, o3])

    assert len(groups[0.5]) == 2
    assert len(groups[0.25]) == 1


def test_group_orders_treats_near_equal_prices_as_same_bucket():
    o1 = _order(0.5000001, "c1", "h1")
    o2 = _order(0.5000002, "c2", "h2")

    groups = group_orders_for_batch([o1, o2])

    assert len(groups) == 1
    assert len(next(iter(groups.values()))) == 2


def test_registration_limit_price_never_exceeds_target():
    # spot 0.495, target 0.5, 5% slippage -> capped at target
    assert registration_limit_price(0.495, 0.5, 0.05) == 0.5
    # spot 0.48, target 0.5, 1% slippage -> slippage cap below target
    assert registration_limit_price(0.48, 0.5, 0.01) == pytest.approx(0.4848)
    # spot 0.50, target 0.5, any slippage -> never above target
    assert registration_limit_price(0.50, 0.5, 0.05) == 0.5


def test_registration_limit_price_zero_slippage():
    assert registration_limit_price(0.48, 0.5, 0.0) == 0.48
