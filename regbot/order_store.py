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
        return [o for o in self.list_all() if o.is_active()]

    def list_active_for_user(self, user_id: int, *, limit: int | None = None) -> list[RegistrationOrder]:
        orders = [o for o in self.list_for_user(user_id, statuses={OrderStatus.ACTIVE}) if o.is_active()]
        if limit is not None:
            orders = orders[:limit]
        return orders

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

    def find_active_for_user(self, user_id: int, id_prefix: str) -> list[RegistrationOrder]:
        return [o for o in self.find_for_user(user_id, id_prefix) if o.is_active()]

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

    def purge_excess_history(self, *, keep: int, user_id: int | None = None) -> int:
        """Delete inactive order files beyond the recent-history limit.

        Always keeps every active order file. For each user, keeps the newest
        `keep` inactive orders and deletes the rest.
        """
        if keep < 0:
            raise ValueError("keep must be >= 0")

        orders = self.list_all()
        active_ids = {o.id for o in orders if o.status not in INACTIVE_STATUSES}

        inactive_by_user: dict[int, list[RegistrationOrder]] = {}
        for order in orders:
            if order.status not in INACTIVE_STATUSES:
                continue
            if user_id is not None and order.user_id != user_id:
                continue
            inactive_by_user.setdefault(order.user_id, []).append(order)

        keep_ids = set(active_ids)
        for user_orders in inactive_by_user.values():
            user_orders.sort(key=lambda o: o.created_at, reverse=True)
            for order in user_orders[:keep]:
                keep_ids.add(order.id)

        removed = 0
        for order in orders:
            if order.id in keep_ids:
                continue
            if order.status not in INACTIVE_STATUSES:
                continue
            if user_id is not None and order.user_id != user_id:
                continue
            if self.delete(order.id):
                removed += 1
        return removed
