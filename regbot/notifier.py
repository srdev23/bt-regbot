from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

from telegram.constants import ParseMode
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut

logger = logging.getLogger(__name__)

_MAX_RETRIES = 4


class TelegramNotifier:
    """Send Telegram messages with per-chat rate limiting."""

    def __init__(self, *, chat_min_interval_sec: float = 1.2) -> None:
        self._app = None
        self._chat_min_interval = chat_min_interval_sec
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._last_call: dict[int, float] = {}

    def bind(self, app) -> None:
        self._app = app

    def _lock_for(self, chat_id: int) -> asyncio.Lock:
        if chat_id not in self._chat_locks:
            self._chat_locks[chat_id] = asyncio.Lock()
        return self._chat_locks[chat_id]

    async def _throttle(self, chat_id: int) -> None:
        """Enforce minimum gap between API calls to the same chat."""
        now = time.monotonic()
        last = self._last_call.get(chat_id, 0.0)
        wait = self._chat_min_interval - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)

    def _mark_called(self, chat_id: int) -> None:
        self._last_call[chat_id] = time.monotonic()

    async def send(self, chat_id: int, text: str) -> None:
        if self._app is None:
            return
        async with self._lock_for(chat_id):
            await self._throttle(chat_id)
            await self._send_html_with_retry(chat_id=chat_id, text=text)
            self._mark_called(chat_id)

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

        async with self._lock_for(chat_id):
            await self._throttle(chat_id)
            message = await self._send_html_with_retry(chat_id=chat_id, text=text)
            self._mark_called(chat_id)

            if old_message_id is not None:
                await self._throttle(chat_id)
                await self._delete_plain(chat_id, old_message_id)
                self._mark_called(chat_id)

            return message.message_id

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        if self._app is None:
            return
        async with self._lock_for(chat_id):
            await self._throttle(chat_id)
            await self._delete_plain(chat_id, message_id)
            self._mark_called(chat_id)

    async def _delete_plain(self, chat_id: int, message_id: int) -> None:
        try:
            await self._plain_with_retry(
                self._app.bot.delete_message,
                chat_id=chat_id,
                message_id=message_id,
            )
        except TelegramError:
            pass

    async def _send_html_with_retry(self, **kwargs: Any):
        return await self._with_retry(
            lambda: self._html_call(self._app.bot.send_message, **kwargs)
        )

    async def _plain_with_retry(self, method: Callable[..., Awaitable[Any]], /, **kwargs: Any):
        return await self._with_retry(lambda: method(**kwargs))

    async def _with_retry(self, call: Callable[[], Awaitable[Any]]):
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                return await call()
            except RetryAfter as exc:
                wait = float(exc.retry_after) + 1.0
                logger.warning("Telegram rate limit, waiting %.0fs", wait)
                await asyncio.sleep(wait)
                last_exc = exc
            except (TimedOut, NetworkError) as exc:
                last_exc = exc
                if attempt + 1 >= _MAX_RETRIES:
                    break
                delay = min(2 ** attempt, 8)
                logger.warning(
                    "Telegram timeout, retry %d/%d in %ds", attempt + 1, _MAX_RETRIES, delay
                )
                await asyncio.sleep(delay)
        if last_exc:
            raise last_exc
        raise RuntimeError("Telegram request failed")

    async def _html_call(self, method, /, **kwargs: Any):
        try:
            return await method(
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
                **kwargs,
            )
        except TelegramError as exc:
            if isinstance(exc, (RetryAfter, TimedOut, NetworkError)):
                raise
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
