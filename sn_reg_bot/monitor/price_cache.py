from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from sn_reg_bot.chain.service import ChainService, RegistrationPrice

logger = logging.getLogger(__name__)


@dataclass
class _CacheEntry:
    price: RegistrationPrice
    fetched_at: float


class SubnetPriceCache:
    """Shared registration price cache to minimize RPC calls across users/orders."""

    def __init__(self, chain: ChainService, ttl_sec: float) -> None:
        self._chain = chain
        self._ttl_sec = ttl_sec
        self._cache: dict[int, _CacheEntry] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, netuid: int) -> asyncio.Lock:
        if netuid not in self._locks:
            self._locks[netuid] = asyncio.Lock()
        return self._locks[netuid]

    async def get_price(self, netuid: int, *, force: bool = False) -> RegistrationPrice:
        now = time.monotonic()
        entry = self._cache.get(netuid)
        if not force and entry is not None and (now - entry.fetched_at) < self._ttl_sec:
            return entry.price

        async with self._lock(netuid):
            entry = self._cache.get(netuid)
            if not force and entry is not None and (now - entry.fetched_at) < self._ttl_sec:
                return entry.price
            price = await self._chain.get_registration_price(netuid)
            self._cache[netuid] = _CacheEntry(price=price, fetched_at=time.monotonic())
            return price

    def invalidate(self, netuid: int) -> None:
        self._cache.pop(netuid, None)
