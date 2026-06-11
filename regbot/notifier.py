from __future__ import annotations

from typing import Any

from telegram.constants import ParseMode
from telegram.error import TelegramError


class TelegramNotifier:
    """Send Telegram messages; rotate one price message per order."""

    def __init__(self) -> None:
        self._app = None

    def bind(self, app) -> None:
        self._app = app

    async def send(self, chat_id: int, text: str) -> None:
        if self._app is None:
            return
        await self._html_call(self._app.bot.send_message, chat_id=chat_id, text=text)

    async def refresh_price_message(
        self,
        chat_id: int,
        text: str,
        *,
        old_message_id: int | None = None,
    ) -> int | None:
        """Send a new price message, then delete the previous one."""
        if self._app is None:
            return None

        message = await self._html_call(
            self._app.bot.send_message,
            chat_id=chat_id,
            text=text,
        )
        if old_message_id is not None:
            await self.delete_message(chat_id, old_message_id)
        return message.message_id

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        if self._app is None:
            return
        try:
            await self._app.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError:
            pass

    async def _html_call(self, method, /, **kwargs: Any):
        try:
            return await method(
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                **kwargs,
            )
        except TelegramError:
            kwargs.pop("parse_mode", None)
            kwargs.pop("disable_web_page_preview", None)
            return await method(**kwargs)

    async def reply_html(self, message, text: str, *, reply_markup=None) -> None:
        try:
            await message.reply_text(
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
        except TelegramError:
            await message.reply_text(text, reply_markup=reply_markup)

    async def edit_html(self, query, text: str, *, reply_markup=None) -> None:
        try:
            await query.edit_message_text(
                text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
        except TelegramError:
            await query.edit_message_text(text, reply_markup=reply_markup)
