from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from bittensor_wallet import Wallet
from bittensor_wallet.errors import KeyFileError, PasswordError


class WalletSessionManager:
    """Per-user in-memory wallet unlock cache. Optional encrypted persistence via WalletPasswordStore."""

    def __init__(self, user_id: int) -> None:
        self.user_id = user_id
        self._passwords: dict[str, str] = {}
        self._wallets: dict[tuple[str, str], Wallet] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def set_password(self, wallet_name: str, password: str) -> None:
        previous = self._passwords.get(wallet_name)
        self._passwords[wallet_name] = password
        if previous != password:
            self._drop_cache(wallet_name)
        else:
            for (name, _), wallet in list(self._wallets.items()):
                if name == wallet_name:
                    self.install_password_env(wallet, password)

    def get_password(self, wallet_name: str) -> str | None:
        return self._passwords.get(wallet_name)

    def has_password(self, wallet_name: str) -> bool:
        return wallet_name in self._passwords

    def forget(self, wallet_name: str) -> None:
        wallet = self._any_wallet(wallet_name)
        if wallet is not None:
            self.clear_password_env(wallet)
        self._drop_cache(wallet_name)
        self._passwords.pop(wallet_name, None)

    def forget_all(self) -> None:
        for name in list(self._passwords):
            self.forget(name)

    def _any_wallet(self, wallet_name: str) -> Wallet | None:
        for (name, _), wallet in self._wallets.items():
            if name == wallet_name:
                return wallet
        return None

    def _drop_cache(self, wallet_name: str) -> None:
        for key in [k for k in list(self._wallets) if k[0] == wallet_name]:
            self._wallets.pop(key, None)

    def _lock(self, wallet_name: str) -> asyncio.Lock:
        if wallet_name not in self._locks:
            self._locks[wallet_name] = asyncio.Lock()
        return self._locks[wallet_name]

    @asynccontextmanager
    async def signing(self, wallet_name: str):
        async with self._lock(wallet_name):
            yield

    @staticmethod
    def clear_password_env(wallet: Wallet) -> None:
        try:
            wallet.coldkey_file.remove_password_from_env()
        except Exception:
            pass
        try:
            if wallet.hotkey_file.exists_on_device():
                wallet.hotkey_file.remove_password_from_env()
        except Exception:
            pass

    @staticmethod
    def install_password_env(wallet: Wallet, password: str) -> None:
        wallet.coldkey_file.save_password_to_env(password)
        if wallet.hotkey_file.exists_on_device() and wallet.hotkey_file.is_encrypted():
            wallet.hotkey_file.save_password_to_env(password)

    def _password_for(self, wallet_name: str) -> str:
        if wallet_name not in self._passwords:
            raise ValueError(f"Wallet '{wallet_name}' is locked.")
        return self._passwords[wallet_name]

    def unlock(self, wallet_name: str, hotkey: str, wallet_path: str) -> Wallet:
        key = (wallet_name, hotkey)
        password = self._password_for(wallet_name)

        cached = self._wallets.get(key)
        if cached is not None:
            self.install_password_env(cached, password)
            return cached

        wallet = Wallet(name=wallet_name, hotkey=hotkey, path=wallet_path)
        try:
            wallet.get_coldkey(password=password)
            if wallet.hotkey_file.exists_on_device() and wallet.hotkey_file.is_encrypted():
                wallet.get_hotkey(password=password)
            self.install_password_env(wallet, password)
            self._wallets[key] = wallet
            return wallet
        except PasswordError as exc:
            raise ValueError(f"Invalid password for wallet '{wallet_name}'.") from exc
        except KeyFileError as exc:
            raise ValueError(f"Cannot read wallet '{wallet_name}': {exc}") from exc

    def prepare_for_signing(
        self, wallet_name: str, hotkey: str, wallet_path: str
    ) -> Wallet:
        return self.unlock(wallet_name, hotkey, wallet_path)

    def has_prepared_wallet(self, wallet_name: str, hotkey: str) -> bool:
        return (wallet_name, hotkey) in self._wallets

    def try_unlock_passwordless(
        self, wallet_name: str, hotkey: str, wallet_path: str
    ) -> bool:
        wallet = Wallet(name=wallet_name, hotkey=hotkey, path=wallet_path)
        try:
            wallet.get_coldkey(password="")
            if wallet.hotkey_file.exists_on_device() and wallet.hotkey_file.is_encrypted():
                wallet.get_hotkey(password="")
            self._passwords.setdefault(wallet_name, "")
            self.install_password_env(wallet, "")
            self._wallets[(wallet_name, hotkey)] = wallet
            return True
        except (PasswordError, KeyFileError):
            return False

    def validate_and_store(
        self,
        wallet_name: str,
        password: str,
        wallet_path: str,
        probe_hotkey: str = "default",
    ) -> None:
        """Decrypt the coldkey ONCE to validate the password, then store it.

        The coldkey password is shared by every hotkey, so a single decryption is
        enough to validate. Per-hotkey wallets are decrypted lazily at signing
        time (and cached), which keeps unlock cheap even for wallets with many
        hotkeys.
        """
        key = (wallet_name, probe_hotkey)
        wallet = self._wallets.get(key) or Wallet(
            name=wallet_name, hotkey=probe_hotkey, path=wallet_path
        )
        try:
            wallet.get_coldkey(password=password)
            if wallet.hotkey_file.exists_on_device() and wallet.hotkey_file.is_encrypted():
                wallet.get_hotkey(password=password)
            self.install_password_env(wallet, password)
        except PasswordError as exc:
            raise ValueError(f"Invalid password for wallet '{wallet_name}'.") from exc
        except KeyFileError as exc:
            raise ValueError(f"Cannot read wallet '{wallet_name}': {exc}") from exc
        self._passwords[wallet_name] = password
        self._wallets[key] = wallet

    def mark_unlocked(self, wallet_name: str, password: str) -> None:
        """Store an already-validated password without an in-process KDF.

        Used after the password was validated in a worker process. The decrypted
        coldkey is produced lazily on first signing.
        """
        self.set_password(wallet_name, password)

    def unlock_all_hotkeys(
        self, wallet_name: str, hotkeys: list[str], wallet_path: str
    ) -> None:
        if not hotkeys:
            raise ValueError(f"Wallet '{wallet_name}' has no hotkeys.")
        for hotkey in hotkeys:
            self.unlock(wallet_name, hotkey, wallet_path)


class UserWalletSessionManager:
    """Factory for per-user wallet sessions — enforces isolation between Telegram users."""

    def __init__(self) -> None:
        self._sessions: dict[int, WalletSessionManager] = {}

    def for_user(self, user_id: int) -> WalletSessionManager:
        if user_id not in self._sessions:
            self._sessions[user_id] = WalletSessionManager(user_id)
        return self._sessions[user_id]

    def clear_user(self, user_id: int) -> None:
        session = self._sessions.pop(user_id, None)
        if session is not None:
            session.forget_all()
