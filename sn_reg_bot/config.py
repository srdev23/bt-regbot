from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    bittensor_network: str
    data_dir: Path
    slippage_tolerance: float
    price_cache_ttl_sec: float
    monitor_tick_sec: int
    price_notify_interval_sec: int
    telegram_chat_min_interval_sec: float
    order_list_limit: int
    log_level: str
    bot_owner_id: int | None
    owner_users_per_page: int
    owner_alert_cooldown_sec: int
    wallet_secret_key: str | None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"

    @property
    def users_dir(self) -> Path:
        return self.data_dir / "users"

    def user_wallet_path(self, user_id: int) -> Path:
        return self.users_dir / str(user_id) / "wallets"

    @classmethod
    def load(cls) -> Settings:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN is missing. Copy .env.example to .env and set your token."
            )
        base = Path(__file__).resolve().parent.parent
        data_dir = Path(os.getenv("DATA_DIR", base / "data"))
        return cls(
            telegram_bot_token=token,
            bittensor_network=os.getenv("BITTENSOR_NETWORK", "finney"),
            data_dir=data_dir,
            slippage_tolerance=float(os.getenv("SLIPPAGE_TOLERANCE", "0.005")),
            price_cache_ttl_sec=float(os.getenv("PRICE_CACHE_TTL_SEC", "5.0")),
            monitor_tick_sec=int(os.getenv("MONITOR_TICK_SEC", "5")),
            price_notify_interval_sec=int(os.getenv("PRICE_NOTIFY_INTERVAL_SEC", "60")),
            telegram_chat_min_interval_sec=float(
                os.getenv("TELEGRAM_CHAT_MIN_INTERVAL_SEC", "1.2")
            ),
            order_list_limit=int(os.getenv("ORDER_LIST_LIMIT", "15")),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            bot_owner_id=_parse_owner_id(os.getenv("BOT_OWNER_ID", "")),
            owner_users_per_page=int(os.getenv("OWNER_USERS_PER_PAGE", "10")),
            owner_alert_cooldown_sec=int(os.getenv("OWNER_ALERT_COOLDOWN_SEC", "60")),
            wallet_secret_key=_optional_env("WALLET_SECRET_KEY"),
        )


def _optional_env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _parse_owner_id(raw: str) -> int | None:
    value = raw.strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError("BOT_OWNER_ID must be a numeric Telegram user id.") from exc
