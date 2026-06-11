from __future__ import annotations

import logging
from typing import Optional

from bittensor_wallet import Wallet
from bittensor_wallet.errors import KeyFileError, PasswordError

logger = logging.getLogger(__name__)


class WalletSessionManager:
    """Keeps coldkey passwords in memory for unattended registration."""

    def __init__(self) -> None:
        self._passwords: dict[str, str] = {}
        self._unlocked: set[str] = set()

    def set_password(self, wallet_name: str, password: str) -> None:
        self._passwords[wallet_name] = password
        self._unlocked.discard(wallet_name)

    def has_password(self, wallet_name: str) -> bool:
        return wallet_name in self._passwords

    def forget(self, wallet_name: str) -> None:
        self._passwords.pop(wallet_name, None)
        self._unlocked.discard(wallet_name)

    def unlock(self, wallet_name: str, hotkey: str, wallet_path: str | None = None) -> Wallet:
        wallet = Wallet(name=wallet_name, hotkey=hotkey, path=wallet_path)
        password = self._passwords.get(wallet_name, "")
        try:
            wallet.get_coldkey(password=password)
            self._unlocked.add(wallet_name)
            return wallet
        except PasswordError as exc:
            raise ValueError(f"Invalid password for wallet '{wallet_name}'.") from exc
        except KeyFileError as exc:
            raise ValueError(f"Cannot read wallet '{wallet_name}': {exc}") from exc

    def get_wallet(
        self, wallet_name: str, hotkey: str, wallet_path: str | None = None
    ) -> Optional[Wallet]:
        if wallet_name not in self._unlocked and wallet_name not in self._passwords:
            return None
        return self.unlock(wallet_name, hotkey, wallet_path)

    def try_unlock_passwordless(
        self, wallet_name: str, hotkey: str, wallet_path: str | None = None
    ) -> bool:
        wallet = Wallet(name=wallet_name, hotkey=hotkey, path=wallet_path)
        try:
            wallet.get_coldkey(password="")
            self._passwords.setdefault(wallet_name, "")
            self._unlocked.add(wallet_name)
            return True
        except (PasswordError, KeyFileError):
            return False
