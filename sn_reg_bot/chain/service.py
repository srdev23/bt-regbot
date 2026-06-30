from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from bittensor_cli.src.bittensor.balances import Balance
from bittensor_cli.src.bittensor.extrinsics.registration import (
    burned_register_extrinsic,
    is_hotkey_registered,
)
from bittensor_cli.src.bittensor.subtensor_interface import SubtensorInterface
from bittensor_cli.src.bittensor.utils import get_hotkey_pub_ss58
from bittensor_wallet import Wallet
from bittensor_wallet.errors import KeyFileError, PasswordError

from sn_reg_bot.wallet.session import WalletSessionManager

logger = logging.getLogger(__name__)

T = TypeVar("T")
_MAX_RPC_ATTEMPTS = 3
_RECONNECT_BACKOFF_SEC = 2.0


class ChainConnectionError(Exception):
    """Raised when the Bittensor RPC connection cannot be recovered."""


def _is_connection_error(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError, OSError, ConnectionError)):
        return True
    msg = str(exc).lower()
    return any(
        token in msg
        for token in (
            "max retries exceeded",
            "websocket",
            "connection closed",
            "connection reset",
            "read error",
            "broken pipe",
            "timeout",
            "network",
        )
    )


@dataclass(frozen=True)
class RegistrationPrice:
    netuid: int
    burn_tao: float
    burn: Balance


@dataclass(frozen=True)
class RegistrationResult:
    success: bool
    message: str
    extrinsic_id: str | None
    uid: int | None = None
    paid_tao: float | None = None


@dataclass(frozen=True)
class HotkeySubnetStatus:
    hotkey_name: str
    hotkey_ss58: str
    registered_netuids: list[int]


class ChainService:
    def __init__(self, network: str) -> None:
        self.network = network
        self._subtensor: SubtensorInterface | None = None
        self._reconnect_lock = asyncio.Lock()

    async def connect(self) -> None:
        if self._subtensor is None:
            self._subtensor = SubtensorInterface(self.network)
            await self._subtensor.substrate.initialize()
            logger.info("Connected to Bittensor network: %s", self.network)

    async def close(self) -> None:
        if self._subtensor is not None:
            try:
                await self._subtensor.substrate.close()
            except Exception:
                logger.debug("Error closing chain connection", exc_info=True)
            self._subtensor = None

    async def reconnect(self) -> None:
        async with self._reconnect_lock:
            logger.warning("Reconnecting to Bittensor network: %s", self.network)
            await self.close()
            await self.connect()

    async def health_check(self) -> None:
        await self._call(lambda: self.subtensor.substrate.get_chain_head())

    async def _call(self, fn: Callable[[], Awaitable[T]]) -> T:
        last_exc: BaseException | None = None
        for attempt in range(1, _MAX_RPC_ATTEMPTS + 1):
            try:
                await self.connect()
                return await fn()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not _is_connection_error(exc):
                    raise
                last_exc = exc
                logger.warning(
                    "Chain RPC failed (attempt %d/%d): %s",
                    attempt,
                    _MAX_RPC_ATTEMPTS,
                    exc,
                )
                try:
                    await self.reconnect()
                except Exception as reconnect_exc:
                    logger.warning("Reconnect attempt failed: %s", reconnect_exc)
                if attempt < _MAX_RPC_ATTEMPTS:
                    await asyncio.sleep(_RECONNECT_BACKOFF_SEC * attempt)
        raise ChainConnectionError(str(last_exc)) from last_exc

    @property
    def subtensor(self) -> SubtensorInterface:
        if self._subtensor is None:
            raise RuntimeError("ChainService is not connected")
        return self._subtensor

    async def get_registration_price(self, netuid: int) -> RegistrationPrice:
        async def _do() -> RegistrationPrice:
            block_hash = await self.subtensor.substrate.get_chain_head()
            burn_raw = await self.subtensor.get_hyperparameter(
                "Burn", netuid=netuid, block_hash=block_hash
            )
            burn = Balance.from_rao(int(burn_raw)) if burn_raw else Balance(0)
            return RegistrationPrice(netuid=netuid, burn_tao=float(burn.tao), burn=burn)

        return await self._call(_do)

    async def subnet_exists(self, netuid: int) -> bool:
        async def _do() -> bool:
            block_hash = await self.subtensor.substrate.get_chain_head()
            return await self.subtensor.subnet_exists(netuid=netuid, block_hash=block_hash)

        return await self._call(_do)

    async def is_hotkey_registered(self, netuid: int, wallet: Wallet) -> bool:
        hotkey = get_hotkey_pub_ss58(wallet)

        async def _do() -> bool:
            return await is_hotkey_registered(self.subtensor, netuid, hotkey)

        return await self._call(_do)

    async def is_hotkey_registered_ss58(self, netuid: int, hotkey_ss58: str) -> bool:
        async def _do() -> bool:
            return await is_hotkey_registered(self.subtensor, netuid, hotkey_ss58)

        return await self._call(_do)

    async def get_balance_tao(self, wallet: Wallet) -> float:
        async def _do() -> float:
            balance = await self.subtensor.get_balance(wallet.coldkeypub.ss58_address)
            return float(balance.tao)

        return await self._call(_do)

    async def get_coldkey_balance_tao(
        self, wallet_name: str, wallet_path: str, hotkey: str
    ) -> float | None:
        try:
            wallet = Wallet(name=wallet_name, hotkey=hotkey, path=wallet_path)
            return await self.get_balance_tao(wallet)
        except ChainConnectionError as exc:
            logger.warning("Balance fetch failed for wallet %s: %s", wallet_name, exc)
            return None
        except Exception as exc:
            logger.warning("Balance fetch failed for wallet %s: %s", wallet_name, exc)
            return None

    async def get_registered_netuids(self, hotkey_ss58: str) -> list[int]:
        async def _do() -> list[int]:
            block_hash = await self.subtensor.substrate.get_chain_head()
            return await self.subtensor.get_netuids_for_hotkey(
                hotkey_ss58, block_hash=block_hash
            )

        return await self._call(_do)

    async def get_coldkey_status(
        self,
        coldkey_name: str,
        hotkeys: list[str],
        wallet_path: str,
    ) -> list[HotkeySubnetStatus]:
        async def _do() -> list[HotkeySubnetStatus]:
            results: list[HotkeySubnetStatus] = []
            for hotkey_name in hotkeys:
                wallet = Wallet(name=coldkey_name, hotkey=hotkey_name, path=wallet_path)
                ss58 = get_hotkey_pub_ss58(wallet)
                netuids = await self.get_registered_netuids(ss58)
                results.append(
                    HotkeySubnetStatus(
                        hotkey_name=hotkey_name,
                        hotkey_ss58=ss58,
                        registered_netuids=sorted(netuids),
                    )
                )
            return results

        return await self._call(_do)

    async def filter_unregistered_hotkeys(
        self,
        netuid: int,
        wallet_name: str,
        wallet_path: str,
        hotkeys: list[str],
    ) -> list[str]:
        async def _check(hotkey: str) -> tuple[str, bool]:
            wallet = Wallet(name=wallet_name, hotkey=hotkey, path=wallet_path)
            ss58 = get_hotkey_pub_ss58(wallet)
            registered = await is_hotkey_registered(self.subtensor, netuid, ss58)
            return hotkey, not registered

        async def _do() -> list[str]:
            if not hotkeys:
                return []
            results = await asyncio.gather(*(_check(hk) for hk in hotkeys))
            return [hk for hk, available in results if available]

        return await self._call(_do)

    @staticmethod
    def _ensure_wallet_unlocked(wallet: Wallet, password: str | None) -> RegistrationResult | None:
        if password is None:
            return RegistrationResult(
                success=False,
                message="Wallet password not available in session. Use /unlock.",
                extrinsic_id=None,
            )
        try:
            WalletSessionManager.install_password_env(wallet, password)
            wallet.unlock_coldkey()
        except PasswordError:
            return RegistrationResult(
                success=False,
                message="The password used to decrypt your Coldkey Keyfile is invalid.",
                extrinsic_id=None,
            )
        except KeyFileError as exc:
            return RegistrationResult(
                success=False,
                message=f"Cannot read wallet keyfile: {exc}",
                extrinsic_id=None,
            )
        except Exception as exc:
            return RegistrationResult(
                success=False,
                message=f"Wallet unlock failed: {exc}",
                extrinsic_id=None,
            )
        return None

    async def register(
        self,
        wallet: Wallet,
        netuid: int,
        *,
        slippage: float,
        password: str | None = None,
        target_price: float | None = None,
    ) -> RegistrationResult:
        log_ctx = f"SN{netuid} {wallet.name}/{wallet.hotkey_str}"
        try:
            unlock_err = self._ensure_wallet_unlocked(wallet, password)
            if unlock_err is not None:
                logger.warning("Register aborted (%s): %s", log_ctx, unlock_err.message)
                return unlock_err

            price = await self.get_registration_price(netuid)
            if target_price is not None and price.burn_tao > target_price:
                msg = (
                    f"Price moved above target before submit: "
                    f"{price.burn_tao:.6f} > {target_price:.6f} TAO"
                )
                logger.info("Register skipped (%s): %s", log_ctx, msg)
                return RegistrationResult(success=False, message=msg, extrinsic_id=None)

            balance = await self._call(
                lambda: self.subtensor.get_balance(wallet.coldkeypub.ss58_address)
            )
            if balance < price.burn:
                msg = (
                    f"Insufficient balance: {balance.tao:.6f} TAO, "
                    f"need {price.burn.tao:.6f} TAO"
                )
                logger.warning("Register aborted (%s): %s", log_ctx, msg)
                return RegistrationResult(success=False, message=msg, extrinsic_id=None)

            limit_price = price.burn_tao * (1.0 + slippage)
            logger.info(
                "Submitting registration (%s) price=%.6f limit=%.6f",
                log_ctx,
                price.burn_tao,
                limit_price,
            )

            unlock_err = self._ensure_wallet_unlocked(wallet, password)
            if unlock_err is not None:
                return unlock_err

            success, message, ext_id = await self._call(
                lambda: burned_register_extrinsic(
                    self.subtensor,
                    wallet=wallet,
                    netuid=netuid,
                    old_balance=balance,
                    wait_for_inclusion=True,
                    wait_for_finalization=False,
                    limit=limit_price,
                )
            )

            uid = None
            if success:
                block_hash = await self._call(
                    lambda: self.subtensor.substrate.get_chain_head()
                )
                uid_raw = await self._call(
                    lambda: self.subtensor.query(
                        "SubtensorModule",
                        "Uids",
                        [netuid, get_hotkey_pub_ss58(wallet)],
                        block_hash=block_hash,
                    )
                )
                uid = int(uid_raw) if uid_raw is not None else None
                logger.info(
                    "Registration success (%s) uid=%s tx=%s", log_ctx, uid, ext_id
                )
            else:
                logger.warning("Registration failed (%s): %s", log_ctx, message)

            return RegistrationResult(
                success=success,
                message=message,
                extrinsic_id=ext_id,
                uid=uid,
                paid_tao=price.burn_tao if success else None,
            )
        except ChainConnectionError as exc:
            logger.warning("Registration chain error (%s): %s", log_ctx, exc)
            return RegistrationResult(
                success=False,
                message=f"Chain connection lost during registration: {exc}",
                extrinsic_id=None,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Registration exception (%s)", log_ctx)
            return RegistrationResult(
                success=False,
                message=f"Registration error: {exc}",
                extrinsic_id=None,
            )
