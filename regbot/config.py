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
    wallet_path: Path
    orders_dir: Path
    slippage_tolerance: float
    price_notify_interval_sec: int
    order_list_limit: int
    order_history_limit: int
    order_history_days: int
    order_auto_purge: bool

    @classmethod
    def load(cls) -> "Settings":
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN is missing. Copy .env.example to .env and set your token."
            )
        base = Path(__file__).resolve().parent.parent
        return cls(
            telegram_bot_token=token,
            bittensor_network=os.getenv("BITTENSOR_NETWORK", "finney"),
            wallet_path=Path(
                os.getenv("BITTENSOR_WALLET_PATH", Path.home() / ".bittensor" / "wallets")
            ),
            orders_dir=Path(os.getenv("ORDERS_DIR", base / "data" / "orders")),
            slippage_tolerance=float(os.getenv("SLIPPAGE_TOLERANCE", "0.005")),
            price_notify_interval_sec=int(os.getenv("PRICE_NOTIFY_INTERVAL_SEC", "60")),
            order_list_limit=int(os.getenv("ORDER_LIST_LIMIT", "10")),
            order_history_limit=int(os.getenv("ORDER_HISTORY_LIMIT", "15")),
            order_history_days=int(os.getenv("ORDER_HISTORY_DAYS", "30")),
            order_auto_purge=os.getenv("ORDER_AUTO_PURGE", "true").lower() in ("1", "true", "yes"),
        )
