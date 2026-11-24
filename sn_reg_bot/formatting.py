from __future__ import annotations

import html

from sn_reg_bot.models.orders import Order, OrderStatus


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def fmt_tao(amount: float | None) -> str:
    if amount is None:
        return "N/A"
    return f"{amount:.6f} TAO"


def fmt_lock_status(status: str) -> str:
    if status == "unlocked":
        return "🔓 Unlocked"
    if status == "passwordless":
        return "🔓 No password"
    return "🔒 Locked"


def fmt_ss58(ss58: str | None, *, label: str = "Address") -> str:
    """Copyable SS58 line for Telegram HTML (tap <code> to copy)."""
    if not ss58:
        return f"{label}: unavailable"
    return f"{label}: <code>{esc(ss58)}</code>"


def fmt_explorer_link(ss58: str | None) -> str | None:
    if not ss58:
        return None
    # taostats has stable /account/{ss58} pages; paste the same address into tao.app to fund.
    return f'<a href="https://taostats.io/account/{esc(ss58)}">View on taostats</a>'


def fmt_order_summary(order: Order) -> str:
    p = order.params
    status_icon = {
        "active": "🟢",
        "completed": "✅",
        "cancelled": "🚫",
        "failed": "❌",
    }.get(order.status.value, "❓")
    price = fmt_tao(order.last_price) if order.last_price is not None else "—"
    target_hit = (
        order.last_price is not None
        and order.status == OrderStatus.ACTIVE
        and order.last_price <= p.max_price_tao
    )
    signal = "🎯" if target_hit else status_icon
    return (
        f"{signal} <code>{order.id[:8]}</code> · SN{p.netuid} · "
        f"{p.coldkey_name}/{p.hotkey_name} · ≤{p.max_price_tao:.4f} TAO · now {price}"
    )


def fmt_order_detail(order: Order) -> str:
    p = order.params
    lines = [
        f"<b>Order</b> <code>{order.id}</code>",
        f"Status: <b>{order.status.value}</b>",
        f"Type: {order.order_type.value}",
        f"Subnet: SN{p.netuid}",
        f"Wallet: {p.coldkey_name}/{p.hotkey_name}",
        f"Target price: ≤ {p.max_price_tao:.6f} TAO",
    ]
    if order.last_price is not None:
        lines.append(f"Last price: {fmt_tao(order.last_price)}")
    if order.last_tx_id:
        lines.append(f"Last tx: <code>{order.last_tx_id}</code>")
    if order.last_error:
        lines.append(f"Last error: {esc(order.last_error)}")
    return "\n".join(lines)


def price_update_message(order: Order, balance: float | None = None) -> str:
    p = order.params
    target_hit = order.last_price is not None and order.last_price <= p.max_price_tao
    icon = "🎯" if target_hit else "👀"
    state = "TARGET HIT" if target_hit else "WATCHING"
    lines = [
        f"{icon} <b>{state}</b> · SN{p.netuid}",
        f"Reg price <b>{fmt_tao(order.last_price)}</b>",
        f"Target ≤ {fmt_tao(p.max_price_tao)}",
        f"{esc(p.coldkey_name)}/{esc(p.hotkey_name)}",
        f"<code>{esc(order.id[:8])}</code>",
    ]
    if balance is not None:
        lines.append(f"Balance {fmt_tao(balance)}")
    return "\n".join(lines)
