from __future__ import annotations

import asyncio
import logging
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from sn_reg_bot.chain.service import ChainService
from sn_reg_bot.config import Settings
from sn_reg_bot.database.repository import OrderRepository, UserRepository
from sn_reg_bot.wallet.manager import (
    HotkeyCreateResult,
    WalletCreateResult,
    WalletManager,
)
from sn_reg_bot.wallet.crypto_worker import PROBE_INVALID, PROBE_OK, probe_password
from sn_reg_bot.wallet.password_store import WalletPasswordStore
from sn_reg_bot.wallet.session import UserWalletSessionManager, WalletSessionManager

logger = logging.getLogger(__name__)


class WalletInUseError(Exception):
    """Raised when a wallet has active orders and cannot be removed."""


@dataclass(frozen=True)
class WalletListEntry:
    name: str
    balance: float | None
    hotkeys: list[str]
    lock_status: str  # unlocked | passwordless | locked


class WalletService:
    """Business logic for user-scoped wallet operations."""

    def __init__(
        self,
        settings: Settings,
        chain: ChainService,
        sessions: UserWalletSessionManager,
        users: UserRepository,
        orders: OrderRepository | None = None,
        password_store: WalletPasswordStore | None = None,
    ) -> None:
        self.settings = settings
        self.chain = chain
        self.sessions = sessions
        self.users = users
        self.orders = orders
        self.password_store = password_store
        self._kdf_pool: ProcessPoolExecutor | None = None

    def _ensure_kdf_pool(self) -> ProcessPoolExecutor:
        if self._kdf_pool is None:
            ctx = multiprocessing.get_context("spawn")
            self._kdf_pool = ProcessPoolExecutor(max_workers=2, mp_context=ctx)
        return self._kdf_pool

    def shutdown(self) -> None:
        if self._kdf_pool is not None:
            self._kdf_pool.shutdown(wait=False, cancel_futures=True)
            self._kdf_pool = None

    async def _probe_password_async(
        self, wallet_name: str, probe_hotkey: str, wallet_path: str, password: str
    ) -> None:
        """Validate the coldkey password in a worker process (never blocks the loop)."""
        loop = asyncio.get_running_loop()
        try:
            pool = self._ensure_kdf_pool()
            result = await loop.run_in_executor(
                pool, probe_password, wallet_name, probe_hotkey, wallet_path, password
            )
        except Exception as exc:
            logger.warning(
                "KDF worker pool unavailable (%s); validating in a thread instead", exc
            )
            self.shutdown()
            result = await asyncio.to_thread(
                probe_password, wallet_name, probe_hotkey, wallet_path, password
            )
        if result == PROBE_OK:
            return
        if result == PROBE_INVALID:
            raise ValueError(f"Invalid password for wallet '{wallet_name}'.")
        detail = result[len("error:") :] if result.startswith("error:") else result
        raise ValueError(f"Cannot unlock wallet '{wallet_name}': {detail}")

    def manager_for(self, user_id: int) -> WalletManager:
        return WalletManager(self.settings.user_wallet_path(user_id), user_id)

    async def ensure_user(self, user_id: int) -> None:
        await self.users.ensure_user(user_id)
        self.settings.user_wallet_path(user_id).mkdir(parents=True, exist_ok=True)

    def get_lock_status(self, user_id: int, coldkey_name: str) -> str:
        session = self.sessions.for_user(user_id)
        if session.has_password(coldkey_name):
            return "unlocked"
        manager = self.manager_for(user_id)
        if not manager.coldkey_requires_password(coldkey_name):
            return "passwordless"
        return "locked"

    async def list_wallets_with_balances(self, user_id: int) -> list[WalletListEntry]:
        await self.ensure_user(user_id)
        manager = self.manager_for(user_id)
        wallets = manager.list_wallets()
        if not wallets:
            return []

        wallet_path = str(self.settings.user_wallet_path(user_id))

        async def _fetch(info) -> WalletListEntry:
            hotkey = info.hotkeys[0] if info.hotkeys else "default"
            balance = await self.chain.get_coldkey_balance_tao(info.name, wallet_path, hotkey)
            return WalletListEntry(
                name=info.name,
                balance=balance,
                hotkeys=info.hotkeys,
                lock_status=self.get_lock_status(user_id, info.name),
            )

        return await asyncio.gather(*(_fetch(w) for w in wallets))

    async def list_wallets_with_balances_legacy(
        self, user_id: int
    ) -> list[tuple[str, float | None, list[str]]]:
        entries = await self.list_wallets_with_balances(user_id)
        return [(e.name, e.balance, e.hotkeys) for e in entries]

    async def get_coldkey_status(self, user_id: int, coldkey_name: str):
        await self.ensure_user(user_id)
        manager = self.manager_for(user_id)
        manager.validate_wallet_access(coldkey_name)
        hotkeys = manager.get_hotkeys(coldkey_name)
        statuses = await self.chain.get_coldkey_status(
            coldkey_name,
            hotkeys,
            str(self.settings.user_wallet_path(user_id)),
        )
        return statuses, self.get_lock_status(user_id, coldkey_name)

    async def create_coldkey(
        self, user_id: int, name: str, password: str, *, overwrite: bool = False
    ) -> WalletCreateResult:
        await self.ensure_user(user_id)
        manager = self.manager_for(user_id)
        result = manager.create_coldkey(name, password, overwrite=overwrite)
        self._commit_unlock(user_id, name, password, manager.get_hotkeys(name) or ["default"])
        await self._persist_password(user_id, name, password)
        return result

    async def import_coldkey_mnemonic(
        self,
        user_id: int,
        name: str,
        mnemonic: str,
        password: str,
        *,
        overwrite: bool = False,
    ) -> WalletCreateResult:
        await self.ensure_user(user_id)
        manager = self.manager_for(user_id)
        result = manager.import_coldkey_from_mnemonic(name, mnemonic, password, overwrite=overwrite)
        hotkeys = manager.get_hotkeys(name)
        if hotkeys:
            self._commit_unlock(user_id, name, password, hotkeys)
            await self._persist_password(user_id, name, password)
        return result

    async def import_wallet_zip(
        self,
        user_id: int,
        zip_path: Path,
        target_name: str,
        *,
        overwrite: bool = False,
    ) -> WalletCreateResult:
        await self.ensure_user(user_id)
        manager = self.manager_for(user_id)
        return manager.import_wallet_from_zip(zip_path, target_name, overwrite=overwrite)

    async def import_hotkey_mnemonic(
        self,
        user_id: int,
        coldkey_name: str,
        hotkey_name: str,
        mnemonic: str,
        *,
        overwrite: bool = False,
    ) -> HotkeyCreateResult:
        await self.ensure_user(user_id)
        manager = self.manager_for(user_id)
        return manager.import_hotkey_from_mnemonic(
            coldkey_name, hotkey_name, mnemonic, overwrite=overwrite
        )

    async def import_hotkey_zip(
        self,
        user_id: int,
        coldkey_name: str,
        hotkey_name: str,
        zip_path: Path,
        *,
        overwrite: bool = False,
    ) -> HotkeyCreateResult:
        await self.ensure_user(user_id)
        manager = self.manager_for(user_id)
        return manager.import_hotkey_from_zip(
            coldkey_name, hotkey_name, zip_path, overwrite=overwrite
        )

    async def create_hotkey(
        self,
        user_id: int,
        coldkey_name: str,
        hotkey_name: str,
        *,
        password: str | None = None,
        overwrite: bool = False,
    ) -> HotkeyCreateResult:
        await self.ensure_user(user_id)
        manager = self.manager_for(user_id)
        result = manager.create_hotkey(
            coldkey_name, hotkey_name, password=password, overwrite=overwrite
        )
        session = self.sessions.for_user(user_id)
        if password:
            self._commit_unlock(
                user_id, coldkey_name, password, [hotkey_name], manager=manager
            )
            await self._persist_password(user_id, coldkey_name, password)
        return result

    def _commit_unlock(
        self,
        user_id: int,
        wallet_name: str,
        password: str,
        hotkeys: list[str],
        *,
        manager: WalletManager | None = None,
    ) -> None:
        manager = manager or self.manager_for(user_id)
        wallet_path = str(manager.wallet_path)
        session = self.sessions.for_user(user_id)
        probe_hotkey = hotkeys[0] if hotkeys else "default"
        session.validate_and_store(wallet_name, password, wallet_path, probe_hotkey)

    async def _persist_password(
        self, user_id: int, wallet_name: str, password: str
    ) -> None:
        if self.password_store is None or not self.password_store.enabled:
            return
        await self.password_store.save(user_id, wallet_name, password)

    def unlock_wallet(
        self, user_id: int, wallet_name: str, password: str
    ) -> list[str]:
        """Synchronous unlock (validates a single KDF in-process). Used at startup/tests."""
        manager = self.manager_for(user_id)
        manager.validate_wallet_access(wallet_name)
        hotkeys = manager.get_hotkeys(wallet_name)
        if not hotkeys:
            hotkeys = ["default"]
        self._commit_unlock(user_id, wallet_name, password, hotkeys, manager=manager)
        logger.info("Unlocked wallet user=%s name=%s hotkeys=%d", user_id, wallet_name, len(hotkeys))
        return hotkeys

    async def unlock_wallet_async(
        self, user_id: int, wallet_name: str, password: str
    ) -> list[str]:
        """Validate the password in a worker process, then store it and persist.

        Never blocks the event loop, so multiple users can unlock simultaneously.
        """
        manager = self.manager_for(user_id)
        manager.validate_wallet_access(wallet_name)
        hotkeys = manager.get_hotkeys(wallet_name) or ["default"]
        await self._probe_password_async(
            wallet_name, hotkeys[0], str(manager.wallet_path), password
        )
        self.sessions.for_user(user_id).mark_unlocked(wallet_name, password)
        await self._persist_password(user_id, wallet_name, password)
        logger.info(
            "Unlocked wallet user=%s name=%s hotkeys=%d", user_id, wallet_name, len(hotkeys)
        )
        return hotkeys

    async def unlock_wallet_and_persist(
        self, user_id: int, wallet_name: str, password: str
    ) -> list[str]:
        return await self.unlock_wallet_async(user_id, wallet_name, password)

    async def try_unlock_passwordless_async(
        self, user_id: int, wallet_name: str
    ) -> bool:
        """Attempt passwordless unlock in a worker process (single KDF probe)."""
        manager = self.manager_for(user_id)
        if not manager.coldkey_exists(wallet_name):
            return False
        hotkeys = manager.get_hotkeys(wallet_name) or ["default"]
        try:
            await self._probe_password_async(
                wallet_name, hotkeys[0], str(manager.wallet_path), ""
            )
        except ValueError:
            return False
        self.sessions.for_user(user_id).mark_unlocked(wallet_name, "")
        return True

    async def lock_wallet(self, user_id: int, wallet_name: str) -> None:
        manager = self.manager_for(user_id)
        manager.validate_wallet_access(wallet_name)
        self.sessions.for_user(user_id).forget(wallet_name)
        if self.password_store is not None:
            await self.password_store.delete(user_id, wallet_name)
        logger.info("Locked wallet user=%s name=%s", user_id, wallet_name)

    async def restore_saved_passwords(self) -> int:
        """Reload encrypted passwords into memory after bot restart."""
        if self.password_store is None or not self.password_store.enabled:
            return 0
        restored = 0
        for record in await self.password_store.list_all():
            manager = self.manager_for(record.user_id)
            if not manager.coldkey_exists(record.wallet_name):
                await self.password_store.delete(record.user_id, record.wallet_name)
                continue
            try:
                password = self.password_store.decrypt_record(record)
                hotkeys = manager.get_hotkeys(record.wallet_name) or ["default"]
                self._commit_unlock(
                    record.user_id,
                    record.wallet_name,
                    password,
                    hotkeys,
                    manager=manager,
                )
                restored += 1
            except Exception as exc:
                logger.warning(
                    "Could not restore wallet user=%s name=%s: %s",
                    record.user_id,
                    record.wallet_name,
                    exc,
                )
        if restored:
            logger.info("Restored %d wallet session(s) from encrypted storage", restored)
        return restored

    def try_unlock_passwordless(self, user_id: int, wallet_name: str) -> bool:
        manager = self.manager_for(user_id)
        if not manager.coldkey_exists(wallet_name):
            return False
        # The coldkey password is shared across hotkeys, so a single probe with an
        # empty password is enough — looping every hotkey would do redundant KDFs.
        hotkeys = manager.get_hotkeys(wallet_name) or ["default"]
        session = self.sessions.for_user(user_id)
        return session.try_unlock_passwordless(
            wallet_name, hotkeys[0], str(manager.wallet_path)
        )

    def validate_wallet_ownership(self, user_id: int, coldkey_name: str) -> None:
        manager = self.manager_for(user_id)
        manager.validate_wallet_access(coldkey_name)

    async def filter_unregistered_hotkeys(
        self,
        user_id: int,
        coldkey_name: str,
        netuid: int,
    ) -> list[str]:
        manager = self.manager_for(user_id)
        manager.validate_wallet_access(coldkey_name)
        hotkeys = manager.get_hotkeys(coldkey_name)
        if not hotkeys:
            return []
        wallet_path = str(self.settings.user_wallet_path(user_id))
        return await self.chain.filter_unregistered_hotkeys(
            netuid, coldkey_name, wallet_path, hotkeys
        )

    async def filter_hotkeys_for_new_order(
        self,
        user_id: int,
        coldkey_name: str,
        netuid: int,
    ) -> list[str]:
        """Hotkeys not registered on subnet and without an active registration order."""
        hotkeys = await self.filter_unregistered_hotkeys(user_id, coldkey_name, netuid)
        if not hotkeys or self.orders is None:
            return hotkeys
        active = await self.orders.list_for_user(user_id, active_only=True, limit=100)
        busy = {
            o.params.hotkey_name
            for o in active
            if o.params.coldkey_name == coldkey_name and o.params.netuid == netuid
        }
        return [hk for hk in hotkeys if hk not in busy]

    async def remove_wallet(self, user_id: int, coldkey_name: str) -> None:
        await self.ensure_user(user_id)
        manager = self.manager_for(user_id)
        manager.validate_wallet_access(coldkey_name)

        if self.orders is not None:
            active = await self.orders.list_active_for_coldkey(user_id, coldkey_name)
            if active:
                raise WalletInUseError(
                    f"Cannot remove wallet '{coldkey_name}': "
                    f"{len(active)} active order(s) still use it. Cancel them first."
                )

        manager.remove_wallet(coldkey_name)
        self.sessions.for_user(user_id).forget(coldkey_name)
        if self.password_store is not None:
            await self.password_store.delete(user_id, coldkey_name)
        logger.info("Removed wallet user=%s name=%s", user_id, coldkey_name)
