from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


class OrderStatus(str, Enum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    FAILED = "failed"


@dataclass
class RegistrationOrder:
    id: str
    user_id: int
    chat_id: int
    netuid: int
    wallet_name: str
    hotkey: str
    reg_at: float
    reg_n: int
    duration_sec: int
    poll_interval_sec: int
    status: OrderStatus = OrderStatus.ACTIVE
    registrations_done: int = 0
    created_at: str = field(default_factory=lambda: iso(utc_now()))
    expires_at: str = ""
    last_price: float | None = None
    last_price_at: str | None = None
    last_price_notify_at: str | None = None
    last_error: str | None = None
    last_tx_id: str | None = None

    @classmethod
    def create(
        cls,
        *,
        user_id: int,
        chat_id: int,
        netuid: int,
        wallet_name: str,
        hotkey: str,
        reg_at: float,
        reg_n: int,
        duration_sec: int,
        poll_interval_sec: int,
    ) -> "RegistrationOrder":
        now = utc_now()
        from datetime import timedelta

        return cls(
            id=str(uuid4()),
            user_id=user_id,
            chat_id=chat_id,
            netuid=netuid,
            wallet_name=wallet_name,
            hotkey=hotkey,
            reg_at=reg_at,
            reg_n=reg_n,
            duration_sec=duration_sec,
            poll_interval_sec=poll_interval_sec,
            expires_at=iso(now + timedelta(seconds=duration_sec)),
        )

    def is_expired(self) -> bool:
        return utc_now() >= parse_iso(self.expires_at)

    def remaining_sec(self) -> int:
        return max(0, int((parse_iso(self.expires_at) - utc_now()).total_seconds()))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RegistrationOrder":
        return cls(
            id=data["id"],
            user_id=data["user_id"],
            chat_id=data["chat_id"],
            netuid=data["netuid"],
            wallet_name=data["wallet_name"],
            hotkey=data["hotkey"],
            reg_at=float(data["reg_at"]),
            reg_n=int(data["reg_n"]),
            duration_sec=int(data["duration_sec"]),
            poll_interval_sec=int(data["poll_interval_sec"]),
            status=OrderStatus(data.get("status", OrderStatus.ACTIVE.value)),
            registrations_done=int(data.get("registrations_done", 0)),
            created_at=data.get("created_at", iso(utc_now())),
            expires_at=data["expires_at"],
            last_price=data.get("last_price"),
            last_price_at=data.get("last_price_at"),
            last_price_notify_at=data.get("last_price_notify_at"),
            last_error=data.get("last_error"),
            last_tx_id=data.get("last_tx_id"),
        )
