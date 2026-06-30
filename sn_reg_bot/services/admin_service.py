from __future__ import annotations

from dataclasses import dataclass, field

from sn_reg_bot.config import Settings
from sn_reg_bot.database.repository import OrderRepository, UserRepository
from sn_reg_bot.models.orders import Order
from sn_reg_bot.models.user import UserRecord
from sn_reg_bot.services.wallet_service import WalletService


@dataclass
class UserUsageSummary:
    profile: UserRecord
    wallet_names: list[str] = field(default_factory=list)
    orders: list[Order] = field(default_factory=list)
    order_counts: dict[str, int] = field(default_factory=dict)


@dataclass
class BotUsageReport:
    total_users: int
    global_active_orders: int
    order_counts: dict[str, int]
    access_counts: dict[str, int]
    users: list[UserUsageSummary]


class AdminService:
    def __init__(
        self,
        settings: Settings,
        users: UserRepository,
        orders: OrderRepository,
        wallet_service: WalletService,
    ) -> None:
        self.settings = settings
        self.users = users
        self.orders = orders
        self.wallet_service = wallet_service

    def _discover_user_ids(self, db_user_ids: set[int]) -> set[int]:
        ids = set(db_user_ids)
        users_dir = self.settings.users_dir
        if users_dir.exists():
            for path in users_dir.iterdir():
                if path.is_dir() and path.name.isdigit():
                    ids.add(int(path.name))
        return ids

    async def _build_user_summary(
        self,
        uid: int,
        profile_by_id: dict[int, UserRecord],
        *,
        orders_per_user: int,
        include_orders: bool,
    ) -> UserUsageSummary:
        profile = profile_by_id.get(uid)
        if profile is None:
            profile = UserRecord(telegram_user_id=uid, created_at="—")

        manager = self.wallet_service.manager_for(uid)
        wallet_names = [w.name for w in manager.list_wallets()]
        user_orders = (
            await self.orders.list_for_user(uid, limit=orders_per_user)
            if include_orders
            else []
        )
        counts = await self.orders.count_for_user_by_status(uid)
        return UserUsageSummary(
            profile=profile,
            wallet_names=wallet_names,
            orders=user_orders,
            order_counts=counts,
        )

    async def build_usage_report(
        self,
        *,
        orders_per_user: int = 5,
        include_order_details: bool = True,
    ) -> BotUsageReport:
        profiles = await self.users.list_all()
        profile_by_id = {p.telegram_user_id: p for p in profiles}
        user_ids = sorted(self._discover_user_ids(set(profile_by_id)))

        global_counts = await self.orders.count_by_status()
        access_counts = await self.users.count_by_access_status()
        active_orders = await self.orders.list_active()

        summaries: list[UserUsageSummary] = []
        for uid in user_ids:
            summaries.append(
                await self._build_user_summary(
                    uid,
                    profile_by_id,
                    orders_per_user=orders_per_user,
                    include_orders=include_order_details,
                )
            )

        return BotUsageReport(
            total_users=len(user_ids),
            global_active_orders=len(active_orders),
            order_counts=global_counts,
            access_counts=access_counts,
            users=summaries,
        )

    async def get_user_summary(self, user_id: int) -> UserUsageSummary | None:
        profiles = await self.users.list_all()
        profile_by_id = {p.telegram_user_id: p for p in profiles}
        discovered = self._discover_user_ids(set(profile_by_id))
        if user_id not in discovered and await self.users.get(user_id) is None:
            return None
        return await self._build_user_summary(
            user_id,
            profile_by_id,
            orders_per_user=20,
            include_orders=True,
        )
