from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from telegram.constants import ParseMode
from telegram.error import TelegramError

logger = logging.getLogger(__name__)


@dataclass
class _PricePanel:
    chat_id: int
    message_id: int
    delete_task: asyncio.Task | None = None


class TelegramNotifier:
    """Send Telegram messages; one editable price panel per order."""

    def __init__(self) -> None:
        self._app = None
        self._price_panels: dict[str, _PricePanel] = {}
        self._delete_tasks: set[asyncio.Task] = set()

    def bind(self, app) -> None:
        self._app = app

    async def send(self, chat_id: int, text: str) -> None:
        if self._app is None:
            return
        try:
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except TelegramError:
            try:
                await self._app.bot.send_message(chat_id=chat_id, text=text)
            except TelegramError:
                logger.exception("Failed to send message to chat %s", chat_id)

    async def update_price_panel(
        self,
        order_id: str,
        chat_id: int,
        text: str,
        *,
        lifetime_sec: int,
    ) -> None:
        """Keep a single price message per order; edit in place and refresh lifetime."""
        if self._app is None or lifetime_sec <= 0:
            return

        panel = self._price_panels.get(order_id)
        if panel and panel.delete_task:
            panel.delete_task.cancel()

        message_id = await self._edit_or_send(chat_id, panel.message_id if panel else None, text)
        delete_task = asyncio.create_task(
            self._delete_panel_later(order_id, chat_id, message_id, lifetime_sec),
            name=f"price-panel-{order_id[:8]}",
        )
        self._price_panels[order_id] = _PricePanel(
            chat_id=chat_id, message_id=message_id, delete_task=delete_task
        )
        self._delete_tasks.add(delete_task)
        delete_task.add_done_callback(self._delete_tasks.discard)

    async def clear_price_panel(self, order_id: str) -> None:
        panel = self._price_panels.pop(order_id, None)
        if panel is None:
            return
        if panel.delete_task:
            panel.delete_task.cancel()
        if self._app is None:
            return
        try:
            await self._app.bot.delete_message(chat_id=panel.chat_id, message_id=panel.message_id)
        except TelegramError:
            pass

    async def _edit_or_send(self, chat_id: int, message_id: int | None, text: str) -> int:
        if message_id is not None:
            try:
                await self._app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                return message_id
            except TelegramError as exc:
                logger.debug("Price panel edit failed (%s), sending new message", exc)

        message = await self._app.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return message.message_id

    async def _delete_panel_later(
        self, order_id: str, chat_id: int, message_id: int, delay_sec: int
    ) -> None:
        try:
            await asyncio.sleep(delay_sec)
            await self._app.bot.delete_message(chat_id=chat_id, message_id=message_id)
        except asyncio.CancelledError:
            raise
        except TelegramError:
            pass
        finally:
            current = self._price_panels.get(order_id)
            if current and current.message_id == message_id:
                self._price_panels.pop(order_id, None)

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

    async def shutdown(self) -> None:
        for order_id in list(self._price_panels):
            await self.clear_price_panel(order_id)
        for task in list(self._delete_tasks):
            task.cancel()
        if self._delete_tasks:
            await asyncio.gather(*self._delete_tasks, return_exceptions=True)
        self._delete_tasks.clear()
