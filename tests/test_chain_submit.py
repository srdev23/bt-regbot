import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from sn_reg_bot.chain.service import ChainService
from sn_reg_bot.chain.service import (
    ComposedRegistration,
    NetuidRegistrationSnapshot,
    PreparedSubmission,
)


@pytest.mark.asyncio
async def test_submission_reference_uses_extrinsic_hash_without_block_hash():
    receipt = MagicMock()
    receipt.block_hash = None
    receipt.extrinsic_hash = bytes.fromhex(
        "abcd1234" * 8
    )

    ref = await ChainService._submission_reference(receipt)

    assert ref == f"0x{receipt.extrinsic_hash.hex()}"


@pytest.mark.asyncio
async def test_submission_reference_prefers_block_identifier():
    receipt = MagicMock()
    receipt.block_hash = "0xabc"
    receipt.get_extrinsic_identifier = AsyncMock(return_value="12345-2")

    ref = await ChainService._submission_reference(receipt)

    assert ref == "12345-2"
    receipt.get_extrinsic_identifier.assert_awaited_once()


@pytest.mark.asyncio
async def test_submission_reference_falls_back_when_identifier_fails():
    receipt = MagicMock()
    receipt.block_hash = "0xabc"
    receipt.get_extrinsic_identifier = AsyncMock(
        side_effect=ValueError("block_hash is not set")
    )
    receipt.extrinsic_hash = "deadbeef"

    ref = await ChainService._submission_reference(receipt)

    assert ref == "0xdeadbeef"


@pytest.mark.asyncio
async def test_fire_registration_calls_serializes_same_coldkey():
    class TestChainService(ChainService):
        async def _call(self, fn):
            return await fn()

    service = TestChainService("test")
    active_by_coldkey: dict[str, int] = {}
    max_active_by_coldkey: dict[str, int] = {}
    active_total = 0
    max_active_total = 0
    sent_calls: list[str] = []

    async def sign_and_send_extrinsic(call, wallet, **_kwargs):
        nonlocal active_total, max_active_total
        coldkey = wallet.coldkeypub.ss58_address
        active_by_coldkey[coldkey] = active_by_coldkey.get(coldkey, 0) + 1
        active_total += 1
        max_active_by_coldkey[coldkey] = max(
            max_active_by_coldkey.get(coldkey, 0),
            active_by_coldkey[coldkey],
        )
        max_active_total = max(max_active_total, active_total)
        sent_calls.append(call)
        await asyncio.sleep(0.01)
        active_by_coldkey[coldkey] -= 1
        active_total -= 1
        receipt = MagicMock()
        receipt.block_hash = None
        receipt.extrinsic_hash = call.encode()
        return True, None, receipt

    service._subtensor = MagicMock()
    service._subtensor.sign_and_send_extrinsic = AsyncMock(
        side_effect=sign_and_send_extrinsic
    )
    snapshot = NetuidRegistrationSnapshot(
        netuid=15,
        block_hash="0xblock",
        burn=MagicMock(tao=0.5),
        price_tao=0.5,
        era_={"period": 1, "current": 1},
    )

    def item(coldkey: str, hotkey: str) -> PreparedSubmission:
        wallet = MagicMock()
        wallet.coldkeypub.ss58_address = coldkey
        return PreparedSubmission(
            wallet=wallet,
            password=None,
            limit_price=0.5,
            hotkey_ss58=hotkey,
            unlocked=True,
        )

    composed = [
        ComposedRegistration(item=item("same", "h1"), call="a", log_ctx="a"),
        ComposedRegistration(item=item("same", "h2"), call="b", log_ctx="b"),
        ComposedRegistration(item=item("other", "h3"), call="c", log_ctx="c"),
    ]

    results = await service.fire_registration_calls(snapshot, composed)

    assert [r.extrinsic_id for r in results] == [
        "0x61",
        "0x62",
        "0x63",
    ]
    assert max_active_by_coldkey["same"] == 1
    assert max_active_total > 1
    assert sent_calls.index("a") < sent_calls.index("b")
