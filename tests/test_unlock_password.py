import tempfile
from pathlib import Path

import pytest

from sn_reg_bot.services.wallet_service import WalletService
from sn_reg_bot.wallet.manager import WalletManager
from sn_reg_bot.wallet.session import UserWalletSessionManager


class _NoStore:
    enabled = False

    async def save(self, *args, **kwargs):
        return None


class _FakeChain:
    async def get_coldkey_balance_tao(self, *args, **kwargs):
        return 1.0


def test_wrong_password_stays_locked():
    with tempfile.TemporaryDirectory() as tmp:
        wallet_path = Path(tmp)
        user_id = 1
        mgr = WalletManager(wallet_path, user_id)
        mgr.create_coldkey("ckey", "secret")
        mgr.create_hotkey("ckey", "hk1", password="secret")

        settings = type("S", (), {"user_wallet_path": lambda self, uid: wallet_path})()
        sessions = UserWalletSessionManager()
        ws = WalletService(settings, _FakeChain(), sessions, None)  # type: ignore[arg-type]

        assert ws.get_lock_status(user_id, "ckey") == "locked"
        ws.unlock_wallet(user_id, "ckey", "secret")
        assert ws.get_lock_status(user_id, "ckey") == "unlocked"

        with pytest.raises(ValueError):
            ws.unlock_wallet(user_id, "ckey", "wrong-password")
        assert ws.get_lock_status(user_id, "ckey") == "unlocked"

        sessions.for_user(user_id).forget("ckey")
        with pytest.raises(ValueError):
            ws.unlock_wallet(user_id, "ckey", "wrong-password")
        assert ws.get_lock_status(user_id, "ckey") == "locked"


@pytest.mark.asyncio
async def test_unlock_async_uses_worker_and_validates():
    with tempfile.TemporaryDirectory() as tmp:
        wallet_path = Path(tmp)
        user_id = 1
        mgr = WalletManager(wallet_path, user_id)
        mgr.create_coldkey("ckey", "secret")
        mgr.create_hotkey("ckey", "hk1", password="secret")

        settings = type("S", (), {"user_wallet_path": lambda self, uid: wallet_path})()
        sessions = UserWalletSessionManager()
        ws = WalletService(
            settings, _FakeChain(), sessions, None, password_store=_NoStore()  # type: ignore[arg-type]
        )
        try:
            assert ws.get_lock_status(user_id, "ckey") == "locked"
            await ws.unlock_wallet_async(user_id, "ckey", "secret")
            assert ws.get_lock_status(user_id, "ckey") == "unlocked"

            sessions.for_user(user_id).forget("ckey")
            with pytest.raises(ValueError):
                await ws.unlock_wallet_async(user_id, "ckey", "wrong-password")
            assert ws.get_lock_status(user_id, "ckey") == "locked"
        finally:
            ws.shutdown()
