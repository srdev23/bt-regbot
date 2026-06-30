import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from sn_reg_bot.config import Settings, _parse_owner_id
from sn_reg_bot.database.connection import Database
from sn_reg_bot.database.repository import OrderRepository, UserRepository
from sn_reg_bot.formatting_owner import fmt_owner_status_summary, fmt_users_page
from sn_reg_bot.handlers.common import is_bot_owner
from sn_reg_bot.models.access import UserAccessStatus
from sn_reg_bot.models.orders import Order, SubnetRegistrationParams
from sn_reg_bot.models.user import UserRecord
from sn_reg_bot.services.access_service import AccessService
from sn_reg_bot.services.admin_service import AdminService, BotUsageReport, UserUsageSummary
from sn_reg_bot.services.owner_alerts import OwnerAlertService


def _settings(**overrides) -> Settings:
    defaults = dict(
        telegram_bot_token="x",
        bittensor_network="finney",
        data_dir=Path("/tmp"),
        slippage_tolerance=0.005,
        price_cache_ttl_sec=5.0,
        monitor_tick_sec=5,
        price_notify_interval_sec=60,
        telegram_chat_min_interval_sec=1.2,
        order_list_limit=15,
        log_level="INFO",
        bot_owner_id=1,
        owner_users_per_page=10,
        owner_alert_cooldown_sec=60,
        wallet_secret_key=None,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def test_parse_owner_id_empty():
    assert _parse_owner_id("") is None
    assert _parse_owner_id("   ") is None


def test_parse_owner_id_valid():
    assert _parse_owner_id("123456789") == 123456789


def test_parse_owner_id_invalid():
    with pytest.raises(RuntimeError):
        _parse_owner_id("not-a-number")


def test_is_bot_owner():
    settings = _settings(bot_owner_id=42)
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"settings": settings}))
    assert is_bot_owner(context, 42) is True
    assert is_bot_owner(context, 99) is False


def test_owner_status_summary_is_compact():
    settings = _settings()
    params = SubnetRegistrationParams(15, "cold", "hot", 0.5)
    order = Order.create_subnet_registration(user_id=99, chat_id=100, params=params)
    report = BotUsageReport(
        total_users=1,
        global_active_orders=1,
        order_counts={"active": 1},
        access_counts={"approved": 1},
        users=[
            UserUsageSummary(
                profile=UserRecord(
                    telegram_user_id=99,
                    created_at="2026-01-01",
                    username="alice",
                    first_name="Alice",
                    access_status=UserAccessStatus.APPROVED,
                ),
                wallet_names=["cold"],
                orders=[order],
                order_counts={"active": 1},
            )
        ],
    )
    text = fmt_owner_status_summary(report, settings)
    assert "owner" in text.lower()
    assert "/users" in text
    assert "cold/hot" not in text


def test_users_page_pagination():
    settings = _settings(owner_users_per_page=2)
    users = [
        UserUsageSummary(
            profile=UserRecord(
                telegram_user_id=i,
                created_at="2026-01-01",
                access_status=UserAccessStatus.APPROVED,
            ),
            order_counts={"active": 0},
        )
        for i in range(5)
    ]
    report = BotUsageReport(
        total_users=5,
        global_active_orders=0,
        order_counts={},
        access_counts={"approved": 5},
        users=users,
    )
    page1, total_pages = fmt_users_page(report, page=1, per_page=2)
    page2, _ = fmt_users_page(report, page=2, per_page=2)
    assert total_pages == 3
    assert "page 1/3" in page1
    assert "page 2/3" in page2
    assert page1.count("ID <code>") == 2
    assert page2.count("ID <code>") == 2


@pytest_asyncio.fixture
async def user_repo():
    with tempfile.TemporaryDirectory() as tmp:
        db = Database(Path(tmp) / "test.db")
        await db.connect()
        users = UserRepository(db)
        yield users
        await db.close()


@pytest.mark.asyncio
async def test_upsert_profile_stores_username(user_repo: UserRepository):
    tg_user = SimpleNamespace(
        id=123,
        username="bob",
        first_name="Bob",
        last_name="Smith",
    )
    await user_repo.upsert_profile(tg_user, owner_id=1)
    profile = await user_repo.get(123)
    assert profile is not None
    assert profile.username == "bob"
    assert profile.access_status == UserAccessStatus.PENDING

    tg_user.username = "bobby"
    await user_repo.upsert_profile(tg_user, owner_id=1)
    profile = await user_repo.get(123)
    assert profile is not None
    assert profile.username == "bobby"


@pytest.mark.asyncio
async def test_owner_auto_approved(user_repo: UserRepository):
    tg_user = SimpleNamespace(id=1, username="owner", first_name="O", last_name=None)
    await user_repo.upsert_profile(tg_user, owner_id=1)
    profile = await user_repo.get(1)
    assert profile is not None
    assert profile.access_status == UserAccessStatus.APPROVED


@pytest.mark.asyncio
async def test_access_service_approve(user_repo: UserRepository):
    settings = _settings()
    notifier = SimpleNamespace(send=AsyncMock(return_value=True))
    alerts = OwnerAlertService(settings, notifier)  # type: ignore[arg-type]
    access = AccessService(settings, user_repo, notifier, alerts)  # type: ignore[arg-type]
    await user_repo.upsert_profile(
        SimpleNamespace(id=55, username="u", first_name="U", last_name=None),
        owner_id=1,
    )
    record = await access.approve_user(55)
    assert record is not None
    assert record.access_status == UserAccessStatus.APPROVED


@pytest.mark.asyncio
async def test_admin_service_build_usage_report():
    with tempfile.TemporaryDirectory() as tmp:
        data_dir = Path(tmp)
        db = Database(data_dir / "bot.db")
        await db.connect()
        users = UserRepository(db)
        orders = OrderRepository(db)
        await users.upsert_profile(
            SimpleNamespace(id=10, username="u10", first_name="U", last_name=None),
            owner_id=1,
        )
        params = SubnetRegistrationParams(15, "c", "h", 0.1)
        order = Order.create_subnet_registration(user_id=10, chat_id=1, params=params)
        await orders.save(order)

        settings = _settings(data_dir=data_dir)
        wallet_svc = SimpleNamespace(
            manager_for=lambda uid: SimpleNamespace(list_wallets=lambda: [])
        )
        admin = AdminService(settings, users, orders, wallet_svc)  # type: ignore[arg-type]
        report = await admin.build_usage_report()
        assert report.total_users == 1
        assert report.global_active_orders == 1
        assert report.users[0].profile.username == "u10"
        await db.close()
