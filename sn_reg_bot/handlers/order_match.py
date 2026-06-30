from __future__ import annotations

from sn_reg_bot.models.orders import Order


def resolve_order_by_prefix(orders: list[Order], prefix: str) -> tuple[Order | None, str | None]:
    """Match exactly one order by id prefix, or return an error message."""
    matches = [o for o in orders if o.id.startswith(prefix)]
    if not matches:
        return None, "No order matches that id."
    if len(matches) > 1:
        return None, "Multiple orders match; use a longer prefix."
    return matches[0], None
