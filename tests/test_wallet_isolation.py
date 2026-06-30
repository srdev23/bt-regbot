import tempfile
from pathlib import Path

import pytest

from sn_reg_bot.wallet.discovery import discover_wallets, wallet_exists
from sn_reg_bot.wallet.manager import WalletManager
from sn_reg_bot.wallet.session import UserWalletSessionManager


def test_wallet_paths_isolated_per_user():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        user1_path = base / "users" / "111" / "wallets"
        user2_path = base / "users" / "222" / "wallets"
        mgr1 = WalletManager(user1_path, 111)
        mgr2 = WalletManager(user2_path, 222)

        mgr1.create_coldkey("wallet_a", "password123")
        mgr2.create_coldkey("wallet_b", "password456")

        assert mgr1.coldkey_exists("wallet_a")
        assert not mgr1.coldkey_exists("wallet_b")
        assert mgr2.coldkey_exists("wallet_b")
        assert not mgr2.coldkey_exists("wallet_a")


def test_session_isolation_between_users():
    sessions = UserWalletSessionManager()
    s1 = sessions.for_user(111)
    s2 = sessions.for_user(222)
    s1.set_password("wallet", "pass1")
    assert s1.has_password("wallet")
    assert not s2.has_password("wallet")


def test_no_overwrite_without_flag():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp)
        mgr = WalletManager(path, 1)
        mgr.create_coldkey("test", "pass")
        with pytest.raises(FileExistsError):
            mgr.create_coldkey("test", "pass2")
