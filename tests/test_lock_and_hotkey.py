import tempfile
from pathlib import Path

from sn_reg_bot.formatting import fmt_lock_status
from sn_reg_bot.services.wallet_service import WalletService
from sn_reg_bot.wallet.manager import WalletManager
from sn_reg_bot.wallet.session import UserWalletSessionManager


class _FakeChain:
    async def get_coldkey_balance_tao(self, *args, **kwargs):
        return 1.0


def test_fmt_lock_status():
    assert "Unlocked" in fmt_lock_status("unlocked")
    assert "Locked" in fmt_lock_status("locked")
    assert "No password" in fmt_lock_status("passwordless")


def test_lock_status_states():
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


def test_import_hotkey_from_files():
    with tempfile.TemporaryDirectory() as tmp:
        mgr = WalletManager(Path(tmp), 1)
        mgr.create_coldkey("ckey", "")
        mgr.create_hotkey("ckey", "orig")
        hotkeys_dir = Path(tmp) / "ckey" / "hotkeys"
        result = mgr.import_hotkey_from_files(
            "ckey",
            "imported",
            hotkeys_dir / "orig",
            hotkeys_dir / "origpub.txt",
        )
        assert result.hotkey_name == "imported"
        assert mgr.hotkey_exists("ckey", "imported")
