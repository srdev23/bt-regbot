from __future__ import annotations

import json
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
    FAILED = "failed"


class OrderType(str, Enum):
    SUBNET_REGISTRATION = "subnet_registration"


@dataclass
class SubnetRegistrationParams:
    netuid: int
    coldkey_name: str
    hotkey_name: str
    max_price_tao: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SubnetRegistrationParams:
        return cls(
            netuid=int(data["netuid"]),
            coldkey_name=str(data["coldkey_name"]),
            hotkey_name=str(data["hotkey_name"]),
            max_price_tao=float(data["max_price_tao"]),
        )

    def identity_key(self) -> tuple[str, str, str, int]:
        return (
            OrderType.SUBNET_REGISTRATION.value,
            self.coldkey_name,
            self.hotkey_name,
            self.netuid,
        )


ORDER_PARAM_TYPES: dict[OrderType, type] = {
    OrderType.SUBNET_REGISTRATION: SubnetRegistrationParams,
}


def parse_order_params(order_type: OrderType, raw: dict[str, Any]) -> SubnetRegistrationParams:
    parser = ORDER_PARAM_TYPES.get(order_type)
    if parser is None:
        raise ValueError(f"Unsupported order type: {order_type}")
    return parser.from_dict(raw)


@dataclass
class Order:
    id: str
    user_id: int
    chat_id: int
    order_type: OrderType
    status: OrderStatus
    params: SubnetRegistrationParams
    poll_interval_sec: int
    created_at: str = field(default_factory=lambda: iso(utc_now()))
    updated_at: str = field(default_factory=lambda: iso(utc_now()))
    last_price: float | None = None
    last_price_at: str | None = None
    last_price_notify_at: str | None = None
    price_message_id: int | None = None
    last_error: str | None = None
    last_tx_id: str | None = None

    @classmethod
    def create_subnet_registration(
        cls,
        *,
        user_id: int,
        chat_id: int,
        params: SubnetRegistrationParams,
        poll_interval_sec: int = 0,
    ) -> Order:
        now = iso(utc_now())
        return cls(
            id=str(uuid4()),
            user_id=user_id,
            chat_id=chat_id,
            order_type=OrderType.SUBNET_REGISTRATION,
            status=OrderStatus.ACTIVE,
            params=params,
            poll_interval_sec=poll_interval_sec,
            created_at=now,
            updated_at=now,
        )

    def is_active(self) -> bool:
        return self.status == OrderStatus.ACTIVE

    def touch(self) -> None:
        self.updated_at = iso(utc_now())

    def params_dict(self) -> dict[str, Any]:
        return self.params.to_dict()

    def identity_key(self) -> tuple[int, str, str, str, int]:
        type_key, coldkey, hotkey, netuid = self.params.identity_key()
        return (self.user_id, type_key, coldkey, hotkey, netuid)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Order:
        order_type = OrderType(row["order_type"])
        params_raw = json.loads(row["params_json"])
        params = parse_order_params(order_type, params_raw)
        return cls(
            id=row["id"],
            user_id=int(row["user_id"]),
            chat_id=int(row["chat_id"]),
            order_type=order_type,
            status=OrderStatus(row["status"]),
            params=params,
            poll_interval_sec=int(row["poll_interval_sec"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            last_price=row.get("last_price"),
            last_price_at=row.get("last_price_at"),
            last_price_notify_at=row.get("last_price_notify_at"),
            price_message_id=row.get("price_message_id"),
            last_error=row.get("last_error"),
            last_tx_id=row.get("last_tx_id"),
        )
