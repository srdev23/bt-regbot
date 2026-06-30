import pytest
import pytest_asyncio

from sn_reg_bot.database.connection import Database
from sn_reg_bot.database.repository import OrderRepository, UserRepository
from sn_reg_bot.models.orders import SubnetRegistrationParams
from sn_reg_bot.services.order_service import OrderService
from sn_reg_bot.services.wallet_service import WalletInUseError, WalletService
from sn_reg_bot.wallet.session import UserWalletSessionManager


class _FakeMonitor:
    async def track(self, order_id: str) -> None:
        pass

    async def untrack(self, order_id: str) -> None:
        pass


class _FakeChain:
    async def get_coldkey_balance_tao(self, *args, **kwargs):
        return 1.0


@pytest_asyncio.fixture
async def wallet_service_setup(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.connect()
    users = UserRepository(db)
    orders = OrderRepository(db)
    settings_data = type("S", (), {"user_wallet_path": lambda self, uid: tmp_path / "users" / str(uid) / "wallets"})()
    ws = WalletService(settings_data, _FakeChain(), UserWalletSessionManager(), users, orders)  # type: ignore[arg-type]
    os = OrderService(orders, users, ws, _FakeMonitor())  # type: ignore[arg-type]
    yield ws, os, tmp_path
    await db.close()


@pytest.mark.asyncio
async def test_cannot_remove_wallet_with_active_order(wallet_service_setup):
    ws, os, tmp_path = wallet_service_setup
    user_id = 42
    await ws.create_coldkey(user_id, "ckey", "pass")
    ws.manager_for(user_id).create_hotkey("ckey", "hkey")
    await os.create_subnet_registration_order(
        user_id=user_id,
        chat_id=100,
        netuid=15,
        coldkey_name="ckey",
        hotkey_name="hkey",
        max_price_tao=0.1,
    )
    with pytest.raises(WalletInUseError):
        await ws.remove_wallet(user_id, "ckey")


@pytest.mark.asyncio
async def test_remove_wallet_after_no_active_orders(wallet_service_setup):
    ws, os, tmp_path = wallet_service_setup
    user_id = 42
    await ws.create_coldkey(user_id, "ckey", "pass")
    assert ws.manager_for(user_id).coldkey_exists("ckey")
    await ws.remove_wallet(user_id, "ckey")
    assert not ws.manager_for(user_id).coldkey_exists("ckey")
