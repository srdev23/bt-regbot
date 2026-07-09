from __future__ import annotations

from collections import defaultdict

from sn_reg_bot.models.orders import Order


def price_bucket(max_price_tao: float) -> float:
    """Normalize max price for batch grouping (6 decimal TAO precision)."""
    return round(max_price_tao, 6)


def group_orders_for_batch(orders: list[Order]) -> dict[float, list[Order]]:
    """Group orders that share the same max registration price limit."""
    groups: dict[float, list[Order]] = defaultdict(list)
    for order in orders:
        groups[price_bucket(order.params.max_price_tao)].append(order)
    return dict(groups)
