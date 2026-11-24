from __future__ import annotations

import math

from sn_reg_bot.config import Settings
from sn_reg_bot.formatting import esc, fmt_order_summary
from sn_reg_bot.models.access import UserAccessStatus
from sn_reg_bot.models.orders import OrderStatus
from sn_reg_bot.services.admin_service import BotUsageReport, UserUsageSummary


def _fmt_order_counts(counts: dict[str, int]) -> str:
    parts = []
    for status in OrderStatus:
        count = counts.get(status.value, 0)
        if count:
            parts.append(f"{count} {status.value}")
    return ", ".join(parts) if parts else "none"


def _fmt_access_counts(counts: dict[str, int]) -> str:
    parts = []
    for status in UserAccessStatus:
        count = counts.get(status.value, 0)
        if count:
            parts.append(f"{count} {status.value}")
    return ", ".join(parts) if parts else "none"


def split_telegram_messages(text: str, *, max_len: int = 3900) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines():
        line_len = len(line) + 1
        if current and current_len + line_len > max_len:
            chunks.append("\n".join(current))
            current = [line]
            current_len = line_len
        else:
            current.append(line)
            current_len += line_len
    if current:
        chunks.append("\n".join(current))
    return chunks


def fmt_owner_status_summary(report: BotUsageReport, settings: Settings) -> str:
    return (
        "🛡️ <b>Bot status (owner)</b>\n"
        f"Network: <code>{esc(settings.bittensor_network)}</code>\n"
        f"Monitor tick: {settings.monitor_tick_sec}s · "
        f"Price alerts: every {settings.price_notify_interval_sec}s\n"
        f"Users: <b>{report.total_users}</b> · "
        f"Active orders: <b>{report.global_active_orders}</b>\n"
        f"Orders: {esc(_fmt_order_counts(report.order_counts))}\n"
        f"Access: {esc(_fmt_access_counts(report.access_counts))}\n\n"
        "Use /users [page] for paginated user list.\n"
        "Use /user &lt;id&gt; for one user's full details.\n"
        "Use /applications for pending requests."
    )


def fmt_users_page(
    report: BotUsageReport, *, page: int, per_page: int
) -> tuple[str, int]:
    total = len(report.users)
    total_pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, total_pages))
    start = (page - 1) * per_page
    chunk = report.users[start : start + per_page]

    lines = [
        f"<b>Users</b> (page {page}/{total_pages}, {total} total)\n",
    ]
    for summary in chunk:
        profile = summary.profile
        active = summary.order_counts.get("active", 0)
        lines.append(
            f"• {profile.access_status.label()} <b>{esc(profile.full_label())}</b>\n"
            f"  ID <code>{profile.telegram_user_id}</code> · "
            f"wallets {len(summary.wallet_names)} · "
            f"orders {esc(_fmt_order_counts(summary.order_counts))} · "
            f"active {active}"
        )

    lines.append("\n/user &lt;id&gt; for details · /users [page] to navigate")
    return "\n".join(lines), total_pages


def fmt_user_detail(summary: UserUsageSummary) -> str:
    profile = summary.profile
    wallets = ", ".join(esc(w) for w in summary.wallet_names) if summary.wallet_names else "none"
    lines = [
        f"<b>{esc(profile.full_label())}</b>",
        f"Status: {profile.access_status.label()}",
        f"ID: <code>{profile.telegram_user_id}</code>",
        f"Joined: {esc(profile.created_at)}",
    ]
    if profile.last_seen_at:
        lines.append(f"Last seen: {esc(profile.last_seen_at)}")
    lines.append(f"Wallets ({len(summary.wallet_names)}): {wallets}")
    lines.append(f"Orders: {esc(_fmt_order_counts(summary.order_counts))}")
    if summary.orders:
        lines.append("\nRecent orders:")
        for order in summary.orders:
            lines.append(fmt_order_summary(order))
    else:
        lines.append("\nRecent orders: none")
    return "\n".join(lines)


def fmt_owner_usage_messages(report: BotUsageReport, settings: Settings) -> list[str]:
    """Legacy full dump — kept for compatibility; prefer summary + /users."""
    return [fmt_owner_status_summary(report, settings)]
