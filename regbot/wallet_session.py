from __future__ import annotations

from bittensor_wallet import Wallet
from bittensor_wallet.errors import KeyFileError, PasswordError


class WalletSessionManager:
    """Keeps coldkey passwords and unlocked wallets in memory."""

    def __init__(self) -> None:
        self._passwords: dict[str, str] = {}
        self._wallets: dict[tuple[str, str], Wallet] = {}

    def set_password(self, wallet_name: str, password: str) -> None:
        self._passwords[wallet_name] = password
        self._drop_wallet(wallet_name)

    def has_password(self, wallet_name: str) -> bool:
        return wallet_name in self._passwords

    def forget(self, wallet_name: str) -> None:
        self._passwords.pop(wallet_name, None)
        self._drop_wallet(wallet_name)

    def _drop_wallet(self, wallet_name: str) -> None:
        for key in [k for k in self._wallets if k[0] == wallet_name]:
            self._wallets.pop(key, None)

    def unlock(self, wallet_name: str, hotkey: str, wallet_path: str | None = None) -> Wallet:
        key = (wallet_name, hotkey)
        cached = self._wallets.get(key)
        if cached is not None:
            return cached

        wallet = Wallet(name=wallet_name, hotkey=hotkey, path=wallet_path)
        password = self._passwords.get(wallet_name, "")
        try:
            wallet.get_coldkey(password=password)
            self._wallets[key] = wallet
            return wallet
        except PasswordError as exc:
            raise ValueError(f"Invalid password for wallet '{wallet_name}'.") from exc
        except KeyFileError as exc:
            raise ValueError(f"Cannot read wallet '{wallet_name}': {exc}") from exc

    def get_wallet(
        self, wallet_name: str, hotkey: str, wallet_path: str | None = None
    ) -> Wallet | None:
        if wallet_name not in self._passwords and (wallet_name, hotkey) not in self._wallets:
            return None
        try:
            return self.unlock(wallet_name, hotkey, wallet_path)
        except ValueError:
            return None

    def try_unlock_passwordless(
        self, wallet_name: str, hotkey: str, wallet_path: str | None = None
    ) -> bool:
        wallet = Wallet(name=wallet_name, hotkey=hotkey, path=wallet_path)
        try:
            wallet.get_coldkey(password="")
            self._passwords.setdefault(wallet_name, "")
            self._wallets[(wallet_name, hotkey)] = wallet
            return True
        except (PasswordError, KeyFileError):
            return False
