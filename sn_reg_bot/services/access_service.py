from __future__ import annotations

import logging

from sn_reg_bot.config import Settings
from sn_reg_bot.database.repository import UserRepository
from sn_reg_bot.models.access import UserAccessStatus
from sn_reg_bot.models.user import UserRecord
from sn_reg_bot.notifier import TelegramNotifier
from sn_reg_bot.services.owner_alerts import OwnerAlertService

logger = logging.getLogger(__name__)


class AccessService:
    def __init__(
        self,
        settings: Settings,
        users: UserRepository,
        notifier: TelegramNotifier,
        owner_alerts: OwnerAlertService,
    ) -> None:
        self.settings = settings
        self.users = users
        self.notifier = notifier
        self.owner_alerts = owner_alerts

    def is_owner(self, user_id: int) -> bool:
        return (
            self.settings.bot_owner_id is not None
            and user_id == self.settings.bot_owner_id
        )

    async def get_user(self, user_id: int) -> UserRecord | None:
        return await self.users.get(user_id)

    @property
    def user_repository(self) -> UserRepository:
        return self.users

    async def can_use_bot(self, user_id: int) -> bool:
        if self.is_owner(user_id):
            return True
        return await self.users.is_approved(user_id)

    async def handle_start(
        self, telegram_user, *, chat_id: int
    ) -> tuple[UserRecord, str]:
        """Register/update profile and return (record, welcome_message_html)."""
        record = await self.users.upsert_profile(
            telegram_user, owner_id=self.settings.bot_owner_id
        )

        if self.is_owner(telegram_user.id):
            return record, (
                "👋 <b>SN Registration Bot</b> (owner)\n\n"
                "You have full access and owner commands:\n"
                "/applications — pending access requests\n"
                "/users [page] — paginated user list\n"
                "/user &lt;id&gt; — user details\n"
                "/approve &lt;id&gt; · /suspend &lt;id&gt;\n"
                "/status — bot overview\n\n"
                "Use /help for all commands."
            )

        if record.access_status == UserAccessStatus.APPROVED:
            return record, (
                "👋 <b>SN Registration Bot</b>\n\n"
                "Multi-user Bittensor subnet registration monitor.\n"
                "Your wallets and orders are fully isolated from other users.\n\n"
                "Use /help to see available commands."
            )

        if record.access_status == UserAccessStatus.SUSPENDED:
            return record, (
                "🚫 <b>Access suspended</b>\n\n"
                "Your bot access has been suspended by the administrator.\n"
                "Contact the bot owner if you believe this is a mistake."
            )

        await self._notify_owner_new_application(record)
        return record, (
            "⏳ <b>Access request submitted</b>\n\n"
            "Your request to use this bot has been sent to the owner.\n"
            "You will be notified when access is approved.\n\n"
            "Send /start again later to check your status."
        )

    async def _notify_owner_new_application(self, record: UserRecord) -> None:
        owner_id = self.settings.bot_owner_id
        if owner_id is None:
            return
        await self.owner_alerts.send(
            (
                f"<b>New access request</b>\n"
                f"{record.full_label()}\n"
                f"ID: <code>{record.telegram_user_id}</code>\n\n"
                f"Use /applications or /approve {record.telegram_user_id}"
            ),
            kind=f"apply:{record.telegram_user_id}",
            cooldown_sec=300,
            force=True,
        )

    async def approve_user(self, target_id: int) -> UserRecord | None:
        if self.is_owner(target_id):
            return await self.users.get(target_id)
        record = await self.users.set_access_status(target_id, UserAccessStatus.APPROVED)
        if record is not None:
            await self.notifier.send(
                target_id,
                "✅ <b>Access approved</b>\n\nYou can now use the bot. Send /help to see commands.",
            )
            logger.info("Approved user %s", target_id)
        return record

    async def suspend_user(self, target_id: int) -> UserRecord | None:
        if self.is_owner(target_id):
            return None
        record = await self.users.set_access_status(target_id, UserAccessStatus.SUSPENDED)
        if record is not None:
            await self.notifier.send(
                target_id,
                "🚫 <b>Access suspended</b>\n\nYour bot access has been suspended.",
            )
            logger.info("Suspended user %s", target_id)
        return record
