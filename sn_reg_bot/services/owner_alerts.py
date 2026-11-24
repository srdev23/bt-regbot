from __future__ import annotations

import logging
import time

from sn_reg_bot.config import Settings
from sn_reg_bot.notifier import TelegramNotifier

logger = logging.getLogger(__name__)


class OwnerAlertService:
    """Send rate-limited alerts to the bot owner."""

    def __init__(self, settings: Settings, notifier: TelegramNotifier) -> None:
        self.settings = settings
        self.notifier = notifier
        self._last_sent: dict[str, float] = {}

    @property
    def owner_chat_id(self) -> int | None:
        return self.settings.bot_owner_id

    async def send(
        self,
        message: str,
        *,
        kind: str = "general",
        cooldown_sec: int | None = None,
        force: bool = False,
    ) -> bool:
        chat_id = self.owner_chat_id
        if chat_id is None:
            return False

        cooldown = (
            cooldown_sec
            if cooldown_sec is not None
            else self.settings.owner_alert_cooldown_sec
        )
        if not force and cooldown > 0:
            now = time.monotonic()
            last = self._last_sent.get(kind, 0.0)
            if now - last < cooldown:
                logger.debug("Owner alert suppressed (kind=%s)", kind)
                return False
            self._last_sent[kind] = now

        text = message if message.startswith("🚨") else f"🚨 <b>Owner alert</b>\n{message}"
        return await self.notifier.send(chat_id, text)
