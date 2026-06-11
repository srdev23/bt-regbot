from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from bittensor_cli.src.bittensor.balances import Balance
from bittensor_cli.src.bittensor.extrinsics.registration import (
    burned_register_extrinsic,
    is_hotkey_registered,
)
from bittensor_cli.src.bittensor.subtensor_interface import SubtensorInterface
from bittensor_cli.src.bittensor.utils import get_hotkey_pub_ss58
from bittensor_wallet import Wallet

from regbot.wallet_discovery import WalletInfo

logger = logging.getLogger(__name__)


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


class ChainService:
    def __init__(self, network: str) -> None:
        self.network = network
        self._subtensor: SubtensorInterface | None = None

    async def connect(self) -> None:
        if self._subtensor is None:
            self._subtensor = SubtensorInterface(self.network)
            logger.info("Connected to Bittensor network: %s", self.network)

    async def close(self) -> None:
        if self._subtensor is not None:
            await self._subtensor.substrate.close()
            self._subtensor = None

    @property
    def subtensor(self) -> SubtensorInterface:
        if self._subtensor is None:
            raise RuntimeError("ChainService is not connected")
        return self._subtensor

    async def get_registration_price(self, netuid: int) -> RegistrationPrice:
        block_hash = await self.subtensor.substrate.get_chain_head()
        burn_raw = await self.subtensor.get_hyperparameter(
            "Burn", netuid=netuid, block_hash=block_hash
        )
        burn = Balance.from_rao(int(burn_raw)) if burn_raw else Balance(0)
        return RegistrationPrice(netuid=netuid, burn_tao=float(burn.tao), burn=burn)

    async def subnet_exists(self, netuid: int) -> bool:
        block_hash = await self.subtensor.substrate.get_chain_head()
        return await self.subtensor.subnet_exists(netuid=netuid, block_hash=block_hash)

    async def is_hotkey_registered(self, netuid: int, wallet: Wallet) -> bool:
        hotkey = get_hotkey_pub_ss58(wallet)
        return await is_hotkey_registered(self.subtensor, netuid, hotkey)

    async def get_balance_tao(self, wallet: Wallet) -> float:
        balance = await self.subtensor.get_balance(wallet.coldkeypub.ss58_address)
        return float(balance.tao)

    async def get_coldkey_balance_tao(
        self, wallet_name: str, wallet_path: str, hotkey: str
    ) -> float | None:
        try:
            wallet = Wallet(name=wallet_name, hotkey=hotkey, path=wallet_path)
            return await self.get_balance_tao(wallet)
        except Exception as exc:
            logger.warning("Balance fetch failed for wallet %s: %s", wallet_name, exc)
            return None

    async def fetch_wallet_balances(
        self, wallet_path: str, wallets: list[WalletInfo]
    ) -> dict[str, float | None]:
        async def _fetch(info: WalletInfo) -> tuple[str, float | None]:
            bal = await self.get_coldkey_balance_tao(info.name, wallet_path, info.hotkeys[0])
            return info.name, bal

        if not wallets:
            return {}
        pairs = await asyncio.gather(*(_fetch(w) for w in wallets))
        return dict(pairs)

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

        if not hotkeys:
            return []
        results = await asyncio.gather(*(_check(hk) for hk in hotkeys))
        return [hk for hk, available in results if available]

    async def register(
        self,
        wallet: Wallet,
        netuid: int,
        *,
        slippage: float,
    ) -> RegistrationResult:
        price = await self.get_registration_price(netuid)
        balance = await self.subtensor.get_balance(wallet.coldkeypub.ss58_address)

        if balance < price.burn:
            return RegistrationResult(
                success=False,
                message=(
                    f"Insufficient balance: {balance.tao:.6f} TAO, "
                    f"need {price.burn.tao:.6f} TAO"
                ),
                extrinsic_id=None,
            )

        limit_price = price.burn_tao * (1.0 + slippage)
        success, message, ext_id = await burned_register_extrinsic(
            self.subtensor,
            wallet=wallet,
            netuid=netuid,
            old_balance=balance,
            wait_for_inclusion=True,
            wait_for_finalization=False,
            limit=limit_price,
        )

        uid = None
        if success:
            block_hash = await self.subtensor.substrate.get_chain_head()
            uid_raw = await self.subtensor.query(
                "SubtensorModule",
                "Uids",
                [netuid, get_hotkey_pub_ss58(wallet)],
                block_hash=block_hash,
            )
            uid = int(uid_raw) if uid_raw is not None else None

        return RegistrationResult(
            success=success,
            message=message,
            extrinsic_id=ext_id,
            uid=uid,
            paid_tao=price.burn_tao,
        )
