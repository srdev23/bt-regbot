from __future__ import annotations

import logging
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_user_id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    order_type TEXT NOT NULL,
    status TEXT NOT NULL,
    params_json TEXT NOT NULL,
    poll_interval_sec INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    last_price REAL,
    last_price_at TEXT,
    last_price_notify_at TEXT,
    price_message_id INTEGER,
    last_error TEXT,
    last_tx_id TEXT,
    FOREIGN KEY (user_id) REFERENCES users(telegram_user_id)
);

CREATE INDEX IF NOT EXISTS idx_orders_user_status ON orders(user_id, status);
CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);

CREATE TABLE IF NOT EXISTS wallet_secrets (
    user_id INTEGER NOT NULL,
    wallet_name TEXT NOT NULL,
    secret_blob TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (user_id, wallet_name),
    FOREIGN KEY (user_id) REFERENCES users(telegram_user_id)
);
"""

_MIGRATIONS = (
    "ALTER TABLE orders ADD COLUMN last_price_notify_at TEXT",
    "ALTER TABLE orders ADD COLUMN price_message_id INTEGER",
    "ALTER TABLE users ADD COLUMN username TEXT",
    "ALTER TABLE users ADD COLUMN first_name TEXT",
    "ALTER TABLE users ADD COLUMN last_name TEXT",
    "ALTER TABLE users ADD COLUMN last_seen_at TEXT",
    "ALTER TABLE users ADD COLUMN access_status TEXT NOT NULL DEFAULT 'approved'",
)


class Database:
    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._migrate()
        await self._conn.commit()
        logger.info("Database connected: %s", self.db_path)

    async def _migrate(self) -> None:
        for sql in _MIGRATIONS:
            try:
                await self._conn.execute(sql)
            except aiosqlite.OperationalError:
                pass

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn
