from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

from sn_reg_bot.database.connection import Database
from sn_reg_bot.models.access import UserAccessStatus
from sn_reg_bot.models.orders import Order, OrderStatus, OrderType, iso, utc_now
from sn_reg_bot.models.user import UserRecord

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WalletSecretRecord:
    user_id: int
    wallet_name: str
    secret_blob: str


class UserRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def ensure_user(self, user_id: int) -> None:
        await self._db.conn.execute(
            "INSERT OR IGNORE INTO users (telegram_user_id, created_at) VALUES (?, ?)",
            (user_id, iso(utc_now())),
        )
        await self._db.conn.commit()

    async def upsert_profile(self, telegram_user, *, owner_id: int | None = None) -> UserRecord:
        now = iso(utc_now())
        is_owner = owner_id is not None and telegram_user.id == owner_id
        if owner_id is None:
            initial_status = UserAccessStatus.APPROVED.value
        elif is_owner:
            initial_status = UserAccessStatus.APPROVED.value
        else:
            initial_status = UserAccessStatus.PENDING.value
        await self._db.conn.execute(
            """
            INSERT INTO users (
                telegram_user_id, created_at, username, first_name, last_name,
                last_seen_at, access_status
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(telegram_user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_name=excluded.last_name,
                last_seen_at=excluded.last_seen_at
            """,
            (
                telegram_user.id,
                now,
                telegram_user.username,
                telegram_user.first_name,
                telegram_user.last_name,
                now,
                initial_status,
            ),
        )
        await self._db.conn.commit()
        record = await self.get(telegram_user.id)
        assert record is not None
        return record

    async def set_access_status(
        self, user_id: int, status: UserAccessStatus
    ) -> UserRecord | None:
        await self._db.conn.execute(
            "UPDATE users SET access_status = ? WHERE telegram_user_id = ?",
            (status.value, user_id),
        )
        await self._db.conn.commit()
        return await self.get(user_id)

    async def list_by_access_status(
        self, status: UserAccessStatus, *, limit: int = 100
    ) -> list[UserRecord]:
        cursor = await self._db.conn.execute(
            "SELECT * FROM users WHERE access_status = ? ORDER BY created_at LIMIT ?",
            (status.value, limit),
        )
        rows = await cursor.fetchall()
        return [UserRecord.from_row(dict(r)) for r in rows]

    async def count_by_access_status(self) -> dict[str, int]:
        cursor = await self._db.conn.execute(
            "SELECT access_status, COUNT(*) AS cnt FROM users GROUP BY access_status"
        )
        rows = await cursor.fetchall()
        counts = {status.value: 0 for status in UserAccessStatus}
        for row in rows:
            counts[str(row["access_status"])] = int(row["cnt"])
        return counts

    async def is_approved(self, user_id: int) -> bool:
        record = await self.get(user_id)
        if record is None:
            return False
        return record.access_status == UserAccessStatus.APPROVED

    async def list_all(self) -> list[UserRecord]:
        cursor = await self._db.conn.execute(
            "SELECT * FROM users ORDER BY created_at"
        )
        rows = await cursor.fetchall()
        return [UserRecord.from_row(dict(r)) for r in rows]

    async def get(self, user_id: int) -> UserRecord | None:
        cursor = await self._db.conn.execute(
            "SELECT * FROM users WHERE telegram_user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return UserRecord.from_row(dict(row)) if row else None


class WalletSecretRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def save(self, user_id: int, wallet_name: str, secret_blob: str) -> None:
        now = iso(utc_now())
        await self._db.conn.execute(
            """
            INSERT INTO wallet_secrets (user_id, wallet_name, secret_blob, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, wallet_name) DO UPDATE SET
                secret_blob=excluded.secret_blob,
                updated_at=excluded.updated_at
            """,
            (user_id, wallet_name, secret_blob, now),
        )
        await self._db.conn.commit()

    async def delete(self, user_id: int, wallet_name: str) -> None:
        await self._db.conn.execute(
            "DELETE FROM wallet_secrets WHERE user_id = ? AND wallet_name = ?",
            (user_id, wallet_name),
        )
        await self._db.conn.commit()

    async def list_all(self) -> list[WalletSecretRecord]:
        cursor = await self._db.conn.execute(
            "SELECT user_id, wallet_name, secret_blob FROM wallet_secrets"
        )
        rows = await cursor.fetchall()
        return [
            WalletSecretRecord(
                user_id=int(row["user_id"]),
                wallet_name=str(row["wallet_name"]),
                secret_blob=str(row["secret_blob"]),
            )
            for row in rows
        ]


class OrderRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def save(self, order: Order) -> None:
        order.touch()
        await self._db.conn.execute(
            """
            INSERT INTO orders (
                id, user_id, chat_id, order_type, status, params_json,
                poll_interval_sec, created_at, updated_at,
                last_price, last_price_at, last_price_notify_at, price_message_id,
                last_error, last_tx_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,
                params_json=excluded.params_json,
                poll_interval_sec=excluded.poll_interval_sec,
                updated_at=excluded.updated_at,
                last_price=excluded.last_price,
                last_price_at=excluded.last_price_at,
                last_price_notify_at=excluded.last_price_notify_at,
                price_message_id=excluded.price_message_id,
                last_error=excluded.last_error,
                last_tx_id=excluded.last_tx_id
            """,
            (
                order.id,
                order.user_id,
                order.chat_id,
                order.order_type.value,
                order.status.value,
                json.dumps(order.params_dict()),
                order.poll_interval_sec,
                order.created_at,
                order.updated_at,
                order.last_price,
                order.last_price_at,
                order.last_price_notify_at,
                order.price_message_id,
                order.last_error,
                order.last_tx_id,
            ),
        )
        await self._db.conn.commit()
        logger.debug("Saved order %s status=%s user=%s", order.id[:8], order.status.value, order.user_id)

    async def update_max_price_if_active(
        self, user_id: int, order_id: str, max_price_tao: float
    ) -> Order | None:
        order = await self.get_for_user(user_id, order_id)
        if order is None or not order.is_active():
            return None
        params = order.params.to_dict()
        params["max_price_tao"] = max_price_tao
        now = iso(utc_now())
        cursor = await self._db.conn.execute(
            """
            UPDATE orders SET params_json = ?, updated_at = ?
            WHERE id = ? AND user_id = ? AND status = ?
            """,
            (
                json.dumps(params),
                now,
                order_id,
                user_id,
                OrderStatus.ACTIVE.value,
            ),
        )
        await self._db.conn.commit()
        if cursor.rowcount == 0:
            return None
        order.params.max_price_tao = max_price_tao
        order.updated_at = now
        return order

    async def update_price_if_active(
        self, order_id: str, last_price: float, last_price_at: str
    ) -> bool:
        now = iso(utc_now())
        cursor = await self._db.conn.execute(
            """
            UPDATE orders SET last_price = ?, last_price_at = ?, updated_at = ?
            WHERE id = ? AND status = ?
            """,
            (last_price, last_price_at, now, order_id, OrderStatus.ACTIVE.value),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def update_notify_if_active(
        self,
        order_id: str,
        *,
        price_message_id: int,
        last_price_notify_at: str,
    ) -> bool:
        now = iso(utc_now())
        cursor = await self._db.conn.execute(
            """
            UPDATE orders SET price_message_id = ?, last_price_notify_at = ?, updated_at = ?
            WHERE id = ? AND status = ?
            """,
            (
                price_message_id,
                last_price_notify_at,
                now,
                order_id,
                OrderStatus.ACTIVE.value,
            ),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def clear_price_message_if_active(self, order_id: str) -> bool:
        now = iso(utc_now())
        cursor = await self._db.conn.execute(
            """
            UPDATE orders SET price_message_id = NULL, updated_at = ?
            WHERE id = ? AND status = ?
            """,
            (now, order_id, OrderStatus.ACTIVE.value),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def reset_price_alert_tracking_for_active(self) -> int:
        """Clear Telegram alert ids/timestamps so monitor sends fresh messages."""
        now = iso(utc_now())
        cursor = await self._db.conn.execute(
            """
            UPDATE orders SET price_message_id = NULL, last_price_notify_at = NULL, updated_at = ?
            WHERE status = ?
            """,
            (now, OrderStatus.ACTIVE.value),
        )
        await self._db.conn.commit()
        return cursor.rowcount

    async def reset_price_notify_if_active(self, order_id: str) -> bool:
        """Allow an immediate price-alert retry after a failed Telegram send."""
        now = iso(utc_now())
        cursor = await self._db.conn.execute(
            """
            UPDATE orders SET last_price_notify_at = NULL, updated_at = ?
            WHERE id = ? AND status = ?
            """,
            (now, order_id, OrderStatus.ACTIVE.value),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def touch_price_notify_if_active(self, order_id: str) -> bool:
        """Record a failed/deferred alert attempt without clearing the message id."""
        now = iso(utc_now())
        cursor = await self._db.conn.execute(
            """
            UPDATE orders SET last_price_notify_at = ?, updated_at = ?
            WHERE id = ? AND status = ?
            """,
            (now, now, order_id, OrderStatus.ACTIVE.value),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def complete_if_active(
        self, order_id: str, *, last_tx_id: str | None = None
    ) -> bool:
        now = iso(utc_now())
        cursor = await self._db.conn.execute(
            """
            UPDATE orders SET status = ?, last_error = NULL, last_tx_id = ?, updated_at = ?
            WHERE id = ? AND status = ?
            """,
            (
                OrderStatus.COMPLETED.value,
                last_tx_id,
                now,
                order_id,
                OrderStatus.ACTIVE.value,
            ),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def fail_if_active(self, order_id: str, error: str) -> bool:
        now = iso(utc_now())
        cursor = await self._db.conn.execute(
            """
            UPDATE orders SET status = ?, last_error = ?, updated_at = ?
            WHERE id = ? AND status = ?
            """,
            (OrderStatus.FAILED.value, error, now, order_id, OrderStatus.ACTIVE.value),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def set_last_error_if_active(self, order_id: str, error: str) -> bool:
        now = iso(utc_now())
        cursor = await self._db.conn.execute(
            """
            UPDATE orders SET last_error = ?, updated_at = ?
            WHERE id = ? AND status = ?
            """,
            (error, now, order_id, OrderStatus.ACTIVE.value),
        )
        await self._db.conn.commit()
        return cursor.rowcount > 0

    async def get(self, order_id: str) -> Order | None:
        cursor = await self._db.conn.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        )
        row = await cursor.fetchone()
        return Order.from_row(dict(row)) if row else None

    async def get_for_user(self, user_id: int, order_id: str) -> Order | None:
        cursor = await self._db.conn.execute(
            "SELECT * FROM orders WHERE id = ? AND user_id = ?",
            (order_id, user_id),
        )
        row = await cursor.fetchone()
        return Order.from_row(dict(row)) if row else None

    async def list_active(self) -> list[Order]:
        cursor = await self._db.conn.execute(
            "SELECT * FROM orders WHERE status = ? ORDER BY created_at",
            (OrderStatus.ACTIVE.value,),
        )
        rows = await cursor.fetchall()
        return [Order.from_row(dict(r)) for r in rows]

    async def list_for_user(
        self, user_id: int, *, limit: int = 15, active_only: bool = False
    ) -> list[Order]:
        if active_only:
            query = (
                "SELECT * FROM orders WHERE user_id = ? AND status = ? "
                "ORDER BY created_at DESC LIMIT ?"
            )
            params: tuple[Any, ...] = (user_id, OrderStatus.ACTIVE.value, limit)
        else:
            query = (
                "SELECT * FROM orders WHERE user_id = ? "
                "ORDER BY created_at DESC LIMIT ?"
            )
            params = (user_id, limit)
        cursor = await self._db.conn.execute(query, params)
        rows = await cursor.fetchall()
        return [Order.from_row(dict(r)) for r in rows]

    async def has_active_duplicate(self, order: Order) -> bool:
        if order.order_type != OrderType.SUBNET_REGISTRATION:
            return False
        cursor = await self._db.conn.execute(
            """
            SELECT id FROM orders
            WHERE user_id = ? AND status = ? AND order_type = ?
              AND json_extract(params_json, '$.coldkey_name') = ?
              AND json_extract(params_json, '$.hotkey_name') = ?
              AND json_extract(params_json, '$.netuid') = ?
              AND id != ?
            LIMIT 1
            """,
            (
                order.user_id,
                OrderStatus.ACTIVE.value,
                order.order_type.value,
                order.params.coldkey_name,
                order.params.hotkey_name,
                order.params.netuid,
                order.id,
            ),
        )
        row = await cursor.fetchone()
        return row is not None

    async def list_active_for_coldkey(self, user_id: int, coldkey_name: str) -> list[Order]:
        cursor = await self._db.conn.execute(
            """
            SELECT * FROM orders
            WHERE user_id = ? AND status = ?
              AND json_extract(params_json, '$.coldkey_name') = ?
            """,
            (user_id, OrderStatus.ACTIVE.value, coldkey_name),
        )
        rows = await cursor.fetchall()
        return [Order.from_row(dict(r)) for r in rows]

    async def cancel(self, user_id: int, order_id: str) -> Order | None:
        order = await self.get_for_user(user_id, order_id)
        if order is None or not order.is_active():
            return None
        order.status = OrderStatus.CANCELLED
        await self.save(order)
        logger.info("Cancelled order %s user=%s", order_id[:8], user_id)
        return order

    async def count_by_status(self) -> dict[str, int]:
        cursor = await self._db.conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM orders GROUP BY status"
        )
        rows = await cursor.fetchall()
        counts = {status.value: 0 for status in OrderStatus}
        for row in rows:
            counts[str(row["status"])] = int(row["cnt"])
        return counts

    async def count_for_user_by_status(self, user_id: int) -> dict[str, int]:
        cursor = await self._db.conn.execute(
            "SELECT status, COUNT(*) AS cnt FROM orders WHERE user_id = ? GROUP BY status",
            (user_id,),
        )
        rows = await cursor.fetchall()
        counts = {status.value: 0 for status in OrderStatus}
        for row in rows:
            counts[str(row["status"])] = int(row["cnt"])
        return counts
