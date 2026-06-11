from __future__ import annotations

import html

from regbot.models import OrderStatus, RegistrationOrder, parse_iso


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def fmt_tao(amount: float | None) -> str:
    if amount is None:
        return "n/a"
    return f"{amount:.6f} τ"


def fmt_duration(seconds: int) -> str:
    if seconds <= 0:
        return "0s"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs and not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def fmt_duration_short(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def fmt_datetime(iso_value: str | None) -> str:
    if not iso_value:
        return "—"
    try:
        dt = parse_iso(iso_value)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except ValueError:
        return iso_value


def status_badge(status: OrderStatus) -> str:
    icons = {
        OrderStatus.ACTIVE: "🟢",
        OrderStatus.COMPLETED: "✅",
        OrderStatus.CANCELLED: "⛔",
        OrderStatus.EXPIRED: "⏱",
        OrderStatus.FAILED: "❌",
    }
    return f"{icons.get(status, '•')} {status.value}"


def header(title: str, subtitle: str | None = None) -> str:
    lines = [f"<b>{esc(title)}</b>"]
    if subtitle:
        lines.append(f"<i>{esc(subtitle)}</i>")
    return "\n".join(lines)


def divider() -> str:
    return "─────────────────"


def welcome_message() -> str:
    return (
        f"{header('Bittensor Reg Bot', 'Subnet registration monitor & auto-register')}\n\n"
        f"<b>Commands</b>\n"
        f"  /addorder — create a monitored order\n"
        f"  /orders — active orders\n"
        f"  /orders history — recent finished orders\n"
        f"  /order &lt;id&gt; — order details\n"
        f"  /cancelorder &lt;id&gt; — cancel active order\n"
        f"  /clearorders — remove old finished orders\n"
        f"  /price &lt;netuid&gt; — current reg price\n"
        f"  /wallets — list local wallets\n"
        f"  /unlock &lt;wallet&gt; — unlock coldkey for session\n"
        f"  /status — monitoring summary\n\n"
        f"Auto-registers when burn price ≤ your target, with <b>0.5% slippage</b> protection."
    )


def fmt_wallet_line(name: str, balance: float | None, hotkeys: list[str] | None = None) -> str:
    bal = fmt_tao(balance) if balance is not None else "n/a"
    line = f"💼 <b>{esc(name)}</b>  ·  {bal}"
    if hotkeys:
        hk = " ".join(f"<code>{esc(h)}</code>" for h in hotkeys)
        line += f"\n     {hk}"
    return line


def wallet_picker_message(balances: dict[str, float | None], wallets: list[dict]) -> str:
    lines = ["Tap a button or type a wallet name:", ""]
    for w in wallets:
        lines.append(fmt_wallet_line(w["name"], balances.get(w["name"]), w["hotkeys"]))
    return "\n".join(lines)


def addorder_step(step: int, total: int, title: str, body: str) -> str:
    return (
        f"<b>Step {step}/{total} · {esc(title)}</b>\n"
        f"{body}"
    )


def order_card(order: RegistrationOrder, *, detailed: bool = False, balance: float | None = None) -> str:
    price = fmt_tao(order.last_price)
    target_hit = (
        order.last_price is not None
        and order.status == OrderStatus.ACTIVE
        and order.last_price <= order.reg_at
    )
    signal = "🎯 READY" if target_hit else status_badge(order.status)

    lines = [
        f"{signal}  <code>{esc(order.id[:8])}</code>",
        f"  SN{order.netuid} · {esc(order.wallet_name)}/{esc(order.hotkey)}",
    ]
    if balance is not None:
        lines.append(f"  Balance {fmt_tao(balance)}")
    lines.extend(
        [
            f"  Target {fmt_tao(order.reg_at)} · Now {price}",
            f"  Progress {order.registrations_done}/{order.reg_n} · Poll {order.poll_interval_sec}s",
        ]
    )
    if order.status == OrderStatus.ACTIVE:
        lines.append(f"  Time left {fmt_duration(order.remaining_sec())}")
    if detailed:
        lines.extend(
            [
                divider(),
                f"Created {fmt_datetime(order.created_at)}",
                f"Expires {fmt_datetime(order.expires_at)}",
            ]
        )
        if order.last_tx_id:
            lines.append(f"Last tx <code>{esc(order.last_tx_id)}</code>")
        if order.last_error:
            lines.append(f"Last error <i>{esc(order.last_error)}</i>")
    return "\n".join(lines)


def orders_list_title(mode: str, count: int, shown: int) -> str:
    titles = {
        "active": "Active Orders",
        "history": "Order History",
        "all": "All Orders",
    }
    title = titles.get(mode, "Orders")
    extra = f" (showing {shown}/{count})" if shown < count else f" ({count})"
    return f"{header(title)}{extra}"


def price_update_message(
    order: RegistrationOrder, remaining_sec: int, balance: float | None = None
) -> str:
    target_hit = order.last_price is not None and order.last_price <= order.reg_at
    icon = "🎯" if target_hit else "👀"
    state = "TARGET HIT" if target_hit else "WATCHING"
    lines = [
        f"{icon} <b>{state}</b> · SN{order.netuid}",
        f"Reg price <b>{fmt_tao(order.last_price)}</b>",
        f"Target {fmt_tao(order.reg_at)} · {order.registrations_done}/{order.reg_n} done",
    ]
    if balance is not None:
        lines.append(f"Balance {fmt_tao(balance)}")
    lines.extend(
        [
            f"{esc(order.wallet_name)}/{esc(order.hotkey)} · "
            f"⏳ {fmt_duration(remaining_sec)} left",
            f"<code>{esc(order.id[:8])}</code>",
        ]
    )
    return "\n".join(lines)


def price_hit_message(order: RegistrationOrder, price_tao: float, slippage_pct: float) -> str:
    return (
        f"⚡ <b>Price hit — registering</b>\n"
        f"SN{order.netuid} · <code>{esc(order.id[:8])}</code>\n"
        f"Price {fmt_tao(price_tao)} ≤ target {fmt_tao(order.reg_at)}\n"
        f"Slippage {slippage_pct:.1f}% · submitting tx…"
    )


def registration_success_message(
    order: RegistrationOrder,
    *,
    uid: int | None,
    paid_tao: float | None,
    extrinsic_id: str | None,
    message: str,
) -> str:
    lines = [
        f"✅ <b>Registration successful</b>",
        f"SN{order.netuid} · <code>{esc(order.id[:8])}</code>",
        f"{esc(order.wallet_name)}/{esc(order.hotkey)}",
        f"Progress {order.registrations_done}/{order.reg_n}",
    ]
    if paid_tao is not None:
        lines.append(f"Paid {fmt_tao(paid_tao)}")
    if uid is not None:
        lines.append(f"UID <b>{uid}</b>")
    if extrinsic_id:
        lines.append(f"Tx <code>{esc(extrinsic_id)}</code>")
    if message and message not in lines:
        lines.append(f"<i>{esc(message)}</i>")
    return "\n".join(lines)


def order_failed_message(order: RegistrationOrder, reason: str) -> str:
    return (
        f"❌ <b>Order failed</b>\n"
        f"SN{order.netuid} · <code>{esc(order.id[:8])}</code>\n"
        f"{esc(reason)}"
    )


def hotkey_already_registered_message(order: RegistrationOrder) -> str:
    return (
        f"✅ Hotkey <code>{esc(order.hotkey)}</code> already registered on "
        f"SN{order.netuid}.\n"
        f"Order <code>{esc(order.id[:8])}</code> marked complete."
    )


def registration_failed_message(order: RegistrationOrder, error: str) -> str:
    return (
        f"❌ <b>Registration failed</b>\n"
        f"SN{order.netuid} · <code>{esc(order.id[:8])}</code>\n"
        f"{esc(error)}"
    )


def order_completed_message(order: RegistrationOrder) -> str:
    return (
        f"🏁 <b>Order completed</b>\n"
        f"<code>{esc(order.id[:8])}</code> · SN{order.netuid}\n"
        f"{order.registrations_done}/{order.reg_n} registrations done"
    )


def order_expired_message(order: RegistrationOrder) -> str:
    return (
        f"⏱ <b>Order expired</b>\n"
        f"<code>{esc(order.id[:8])}</code> · SN{order.netuid}\n"
        f"{order.registrations_done}/{order.reg_n} registrations done"
    )


def wallet_locked_message(order: RegistrationOrder, price_tao: float) -> str:
    return (
        f"🔐 <b>Wallet locked</b>\n"
        f"SN{order.netuid} ready at {fmt_tao(price_tao)} "
        f"(target {fmt_tao(order.reg_at)})\n"
        f"Unlock <code>{esc(order.wallet_name)}</code> with /unlock"
    )


def confirm_order_message(
    draft: dict, balance: float | None = None, *, slippage_pct: float = 0.5
) -> str:
    lines = [
        f"📋 <b>Confirm order</b>",
        divider(),
        f"Subnet <b>SN{draft['netuid']}</b>",
        f"Wallet <code>{esc(draft['wallet_name'])}/{esc(draft['hotkey'])}</code>",
    ]
    if balance is not None:
        lines.append(f"Balance {fmt_tao(balance)}")
    lines.extend(
        [
            f"Target {fmt_tao(float(draft['reg_at']))}",
            f"Duration <b>{fmt_duration_short(int(draft['duration_sec']))}</b>",
            f"Poll every <b>{draft['poll_interval_sec']}s</b>",
            f"Slippage <b>{slippage_pct:g}%</b>",
        ]
    )
    if balance is not None and balance < float(draft["reg_at"]):
        lines.append(f"\n⚠️ <i>Balance below target — registration may fail if price rises.</i>")
    return "\n".join(lines)


def status_summary(
    active: list[RegistrationOrder],
    balances: dict[str, float | None] | None = None,
) -> str:
    if not active:
        return f"{header('Status', 'No active monitoring')}\n\nUse /addorder to start."
    lines = [header("Monitoring Status", f"{len(active)} active order(s)"), ""]
    for order in active:
        balance = balances.get(order.wallet_name) if balances else None
        lines.append(order_card(order, balance=balance))
        lines.append("")
    return "\n".join(lines).strip()
