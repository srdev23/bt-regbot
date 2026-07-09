from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from bittensor_cli.src.bittensor.balances import Balance
from bittensor_cli.src.bittensor.extrinsics.registration import is_hotkey_registered
from bittensor_cli.src.bittensor.subtensor_interface import SubtensorInterface
from bittensor_cli.src.bittensor.utils import get_hotkey_pub_ss58
from bittensor_wallet import Wallet
from bittensor_wallet.errors import KeyFileError, PasswordError

from sn_reg_bot.wallet.session import WalletSessionManager

logger = logging.getLogger(__name__)

T = TypeVar("T")
_MAX_RPC_ATTEMPTS = 3
_RECONNECT_BACKOFF_SEC = 2.0


def registration_limit_price(
    current_price_tao: float,
    target_price_tao: float,
    slippage: float,
) -> float:
    """Compute register_limit cap: never above the user's target, slippage only above spot."""
    if slippage < 0:
        slippage = 0.0
    slippage_cap = current_price_tao * (1.0 + slippage)
    return min(target_price_tao, slippage_cap)


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
    submitted: bool = False
    hotkey_ss58: str | None = None


@dataclass(frozen=True)
class HotkeySubnetStatus:
    hotkey_name: str
    hotkey_ss58: str
    registered_netuids: list[int]


@dataclass(frozen=True)
class BatchRegistrationItem:
    """One wallet registration prepared for simultaneous batch submit."""

    wallet: Wallet
    password: str | None
    target_price: float
    label: str = ""


@dataclass(frozen=True)
class NetuidRegistrationSnapshot:
    """Chain state captured once per monitor tick — reused for all submits on a subnet."""

    netuid: int
    block_hash: str
    burn: Balance
    price_tao: float
    era_: dict[str, int]


@dataclass(frozen=True)
class PreparedSubmission:
    """Fully prepared registration — wallet unlocked, limit computed."""

    wallet: Wallet
    password: str | None
    limit_price: float
    hotkey_ss58: str
    label: str = ""
    unlocked: bool = False


@dataclass(frozen=True)
class ComposedRegistration:
    """Extrinsic composed from snapshot — ready for immediate sign_and_send."""

    item: PreparedSubmission
    call: object
    log_ctx: str


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

    async def capture_registration_snapshot(self, netuid: int) -> NetuidRegistrationSnapshot:
        """Fetch price, block hash, and era once for a subnet monitor tick."""

        async def _do() -> NetuidRegistrationSnapshot:
            block_hash = await self.subtensor.substrate.get_chain_head()
            burn_raw = await self.subtensor.get_hyperparameter(
                "Burn", netuid=netuid, block_hash=block_hash
            )
            burn = Balance.from_rao(int(burn_raw)) if burn_raw else Balance(0)
            current_block, tempo, blocks_since_last_step = await asyncio.gather(
                self.subtensor.substrate.get_block_number(block_hash=block_hash),
                self.subtensor.get_hyperparameter(
                    "Tempo", netuid=netuid, block_hash=block_hash
                ),
                self.subtensor.query(
                    "SubtensorModule",
                    "BlocksSinceLastStep",
                    [netuid],
                    block_hash=block_hash,
                ),
            )
            tempo_i = int(tempo) if tempo is not None else 360
            blocks_i = (
                int(blocks_since_last_step)
                if blocks_since_last_step is not None
                else 0
            )
            return NetuidRegistrationSnapshot(
                netuid=netuid,
                block_hash=block_hash,
                burn=burn,
                price_tao=float(burn.tao),
                era_={
                    "period": max(1, tempo_i - blocks_i),
                    "current": int(current_block),
                },
            )

        return await self._call(_do)

    async def compose_registration_calls(
        self,
        snapshot: NetuidRegistrationSnapshot,
        items: list[PreparedSubmission],
    ) -> list[RegistrationResult | ComposedRegistration]:
        """Build extrinsics from snapshot. No balance/price fetch — uses snapshot only."""
        if not items:
            return []

        async def _do() -> list[RegistrationResult | ComposedRegistration]:
            limit_rao_cache: dict[float, int] = {}

            async def _compose_one(
                item: PreparedSubmission,
            ) -> RegistrationResult | ComposedRegistration:
                log_ctx = item.label or (
                    f"SN{snapshot.netuid} {item.wallet.name}/{item.wallet.hotkey_str}"
                )
                if not item.unlocked:
                    unlock_err = self._ensure_wallet_unlocked(item.wallet, item.password)
                    if unlock_err is not None:
                        return unlock_err
                if item.limit_price not in limit_rao_cache:
                    limit_rao_cache[item.limit_price] = Balance.from_tao(
                        item.limit_price
                    ).rao
                call = await self.subtensor.substrate.compose_call(
                    call_module="SubtensorModule",
                    call_function="register_limit",
                    call_params={
                        "netuid": snapshot.netuid,
                        "hotkey": item.hotkey_ss58,
                        "limit_price": limit_rao_cache[item.limit_price],
                    },
                    block_hash=snapshot.block_hash,
                )
                return ComposedRegistration(item=item, call=call, log_ctx=log_ctx)

            return list(await asyncio.gather(*(_compose_one(i) for i in items)))

        return await self._call(_do)

    @staticmethod
    async def _submission_reference(ext_receipt) -> str | None:
        """Best-effort tx reference after broadcast (works without inclusion wait)."""
        if ext_receipt is None:
            return None
        if ext_receipt.block_hash is not None:
            try:
                return await ext_receipt.get_extrinsic_identifier()
            except ValueError:
                pass
        ext_hash = ext_receipt.extrinsic_hash
        if ext_hash is None:
            return None
        if isinstance(ext_hash, str):
            return ext_hash if ext_hash.startswith("0x") else f"0x{ext_hash}"
        return f"0x{ext_hash.hex()}"

    async def fire_registration_calls(
        self,
        snapshot: NetuidRegistrationSnapshot,
        items: list[ComposedRegistration],
    ) -> list[RegistrationResult]:
        """Sign and broadcast only — no compose, price, or balance RPC."""
        if not items:
            return []

        async def _do() -> list[RegistrationResult]:
            logger.info(
                "Sending %d registration(s) on SN%d snapshot_price=%.6f",
                len(items),
                snapshot.netuid,
                snapshot.price_tao,
            )

            async def _send(composed: ComposedRegistration) -> RegistrationResult:
                item = composed.item
                try:
                    success, err_msg, ext_receipt = await self.subtensor.sign_and_send_extrinsic(
                        composed.call,
                        item.wallet,
                        wait_for_inclusion=False,
                        wait_for_finalization=False,
                        era=snapshot.era_,
                    )
                    if not success:
                        logger.warning(
                            "Registration failed (%s): %s", composed.log_ctx, err_msg
                        )
                        return RegistrationResult(
                            success=False,
                            message=err_msg or "Registration failed",
                            extrinsic_id=None,
                        )
                    ext_id = await self._submission_reference(ext_receipt)
                    logger.info(
                        "Registration submitted (%s) tx=%s", composed.log_ctx, ext_id
                    )
                    return RegistrationResult(
                        success=True,
                        message="Submitted",
                        extrinsic_id=ext_id,
                        paid_tao=snapshot.price_tao,
                        submitted=True,
                        hotkey_ss58=item.hotkey_ss58,
                    )
                except Exception as exc:
                    logger.exception(
                        "Registration send exception (%s)", composed.log_ctx
                    )
                    return RegistrationResult(
                        success=False,
                        message=f"Registration error: {exc}",
                        extrinsic_id=None,
                    )

            async def _send_group(group: list[tuple[int, ComposedRegistration]]):
                results: list[tuple[int, RegistrationResult]] = []
                for idx, composed in group:
                    results.append((idx, await _send(composed)))
                return results

            by_coldkey: dict[str, list[tuple[int, ComposedRegistration]]] = {}
            for idx, composed in enumerate(items):
                coldkey = composed.item.wallet.coldkeypub.ss58_address
                by_coldkey.setdefault(coldkey, []).append((idx, composed))

            grouped = await asyncio.gather(
                *(_send_group(group) for group in by_coldkey.values())
            )
            ordered: list[RegistrationResult | None] = [None] * len(items)
            for group in grouped:
                for idx, result in group:
                    ordered[idx] = result
            return [
                result
                if result is not None
                else RegistrationResult(
                    success=False,
                    message="Internal submit error",
                    extrinsic_id=None,
                )
                for result in ordered
            ]

        return await self._call(_do)

    async def submit_registrations(
        self,
        snapshot: NetuidRegistrationSnapshot,
        items: list[PreparedSubmission],
    ) -> list[RegistrationResult]:
        """Compose then fire. Prefer compose + fire separately for tighter batch timing."""
        if not items:
            return []

        composed = await self.compose_registration_calls(snapshot, items)
        final: list[RegistrationResult | None] = [None] * len(items)
        to_fire: list[ComposedRegistration] = []
        fire_indices: list[int] = []

        for idx, entry in enumerate(composed):
            if isinstance(entry, RegistrationResult):
                final[idx] = entry
            else:
                fire_indices.append(idx)
                to_fire.append(entry)

        if to_fire:
            fired = await self.fire_registration_calls(snapshot, to_fire)
            for idx, result in zip(fire_indices, fired):
                final[idx] = result

        return [
            r
            if r is not None
            else RegistrationResult(
                success=False, message="Internal submit error", extrinsic_id=None
            )
            for r in final
        ]

    async def lookup_registration_uid(
        self,
        netuid: int,
        hotkey_ss58: str,
        *,
        retries: int = 6,
        retry_delay_sec: float = 2.0,
    ) -> int | None:
        """Poll for UID after fire-and-forget submit (wait_for_inclusion=False)."""

        async def _query_once() -> int | None:
            block_hash = await self.subtensor.substrate.get_chain_head()
            uid_raw = await self.subtensor.query(
                "SubtensorModule",
                "Uids",
                [netuid, hotkey_ss58],
                block_hash=block_hash,
            )
            return int(uid_raw) if uid_raw is not None else None

        for attempt in range(retries):
            try:
                uid = await self._call(_query_once)
            except ChainConnectionError:
                uid = None
            if uid is not None:
                return uid
            if attempt + 1 < retries:
                await asyncio.sleep(retry_delay_sec)
        return None

    async def register(
        self,
        wallet: Wallet,
        netuid: int,
        *,
        slippage: float,
        password: str | None = None,
        target_price: float | None = None,
        snapshot: NetuidRegistrationSnapshot | None = None,
    ) -> RegistrationResult:
        """Submit one registration. Pass snapshot to avoid re-fetching chain state."""
        log_ctx = f"SN{netuid} {wallet.name}/{wallet.hotkey_str}"
        try:
            snap = snapshot or await self.capture_registration_snapshot(netuid)
            if target_price is not None and snap.price_tao > target_price:
                msg = (
                    f"Price moved above target before submit: "
                    f"{snap.price_tao:.6f} > {target_price:.6f} TAO"
                )
                return RegistrationResult(success=False, message=msg, extrinsic_id=None)

            if target_price is None:
                limit_price = snap.price_tao * (1.0 + slippage)
            else:
                limit_price = registration_limit_price(
                    snap.price_tao, target_price, slippage
                )

            hotkey_ss58 = get_hotkey_pub_ss58(wallet)
            prep = PreparedSubmission(
                wallet=wallet,
                password=password,
                limit_price=limit_price,
                hotkey_ss58=hotkey_ss58,
                label=log_ctx,
            )
            results = await self.submit_registrations(snap, [prep])
            result = results[0]
            if not result.success:
                return result

            uid = await self.lookup_registration_uid(netuid, hotkey_ss58)
            if uid is None:
                return RegistrationResult(
                    success=False,
                    message="Transaction submitted but registration is not confirmed yet.",
                    extrinsic_id=result.extrinsic_id,
                    uid=None,
                    paid_tao=snap.price_tao,
                    submitted=True,
                    hotkey_ss58=hotkey_ss58,
                )
            return RegistrationResult(
                success=True,
                message=f"Registered on {netuid} with UID {uid}",
                extrinsic_id=result.extrinsic_id,
                uid=uid,
                paid_tao=snap.price_tao,
                submitted=result.submitted,
                hotkey_ss58=hotkey_ss58,
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
