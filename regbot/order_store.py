from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path

from regbot.models import OrderStatus, RegistrationOrder, parse_iso, utc_now

logger = logging.getLogger(__name__)

INACTIVE_STATUSES = frozenset(
    {
        OrderStatus.COMPLETED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
        OrderStatus.FAILED,
    }
)


class OrderStore:
    def __init__(self, orders_dir: Path) -> None:
        self.orders_dir = orders_dir
        self.orders_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, order_id: str) -> Path:
        return self.orders_dir / f"{order_id}.json"

    def save(self, order: RegistrationOrder) -> None:
        path = self._path(order.id)
        path.write_text(json.dumps(order.to_dict(), indent=2), encoding="utf-8")

    def load(self, order_id: str) -> RegistrationOrder | None:
        path = self._path(order_id)
        if not path.exists():
            return None
        return RegistrationOrder.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def delete(self, order_id: str) -> bool:
        path = self._path(order_id)
        if path.exists():
            path.unlink()
            return True
        return False

    def list_all(self) -> list[RegistrationOrder]:
        orders: list[RegistrationOrder] = []
        for path in sorted(self.orders_dir.glob("*.json")):
            try:
                orders.append(
                    RegistrationOrder.from_dict(json.loads(path.read_text(encoding="utf-8")))
                )
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                logger.warning("Skipping corrupt order file %s: %s", path, exc)
        return orders

    def list_active(self) -> list[RegistrationOrder]:
        return [
            o
            for o in self.list_all()
            if o.status == OrderStatus.ACTIVE and not o.is_expired()
        ]

    def list_for_user(
        self,
        user_id: int,
        *,
        statuses: set[OrderStatus] | None = None,
        limit: int | None = None,
    ) -> list[RegistrationOrder]:
        orders = [o for o in self.list_all() if o.user_id == user_id]
        if statuses is not None:
            orders = [o for o in orders if o.status in statuses]
        orders.sort(key=lambda o: o.created_at, reverse=True)
        if limit is not None:
            orders = orders[:limit]
        return orders

    def find_for_user(self, user_id: int, id_prefix: str) -> list[RegistrationOrder]:
        prefix = id_prefix.lower()
        return [
            o
            for o in self.list_for_user(user_id)
            if o.id.lower().startswith(prefix)
        ]

    def purge_inactive(
        self,
        *,
        user_id: int | None = None,
        older_than_days: int | None = None,
        all_inactive: bool = False,
    ) -> int:
        cutoff = None
        if older_than_days is not None and older_than_days > 0:
            cutoff = utc_now() - timedelta(days=older_than_days)

        removed = 0
        for order in self.list_all():
            if user_id is not None and order.user_id != user_id:
                continue
            if order.status not in INACTIVE_STATUSES:
                continue
            if not all_inactive and cutoff is not None:
                try:
                    created = parse_iso(order.created_at)
                except ValueError:
                    continue
                if created > cutoff:
                    continue
            if self.delete(order.id):
                removed += 1
        return removed

    def purge_stale_globally(self, older_than_days: int) -> int:
        return self.purge_inactive(older_than_days=older_than_days, all_inactive=False)
