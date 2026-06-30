import asyncio
import time

import pytest

from sn_reg_bot.chain.service import RegistrationPrice
from sn_reg_bot.monitor.price_cache import SubnetPriceCache


class FakeChain:
    def __init__(self):
        self.calls = 0

    async def get_registration_price(self, netuid: int) -> RegistrationPrice:
        self.calls += 1
        from bittensor_cli.src.bittensor.balances import Balance

        return RegistrationPrice(netuid=netuid, burn_tao=0.1, burn=Balance.from_tao(0.1))


@pytest.mark.asyncio
async def test_price_cache_reduces_rpc_calls():
    chain = FakeChain()
    cache = SubnetPriceCache(chain, ttl_sec=10.0)  # type: ignore[arg-type]

    p1 = await cache.get_price(15)
    p2 = await cache.get_price(15)
    assert p1.burn_tao == p2.burn_tao
    assert chain.calls == 1


@pytest.mark.asyncio
async def test_price_cache_force_refresh():
    chain = FakeChain()
    cache = SubnetPriceCache(chain, ttl_sec=10.0)  # type: ignore[arg-type]

    await cache.get_price(15)
    await cache.get_price(15, force=True)
    assert chain.calls == 2
