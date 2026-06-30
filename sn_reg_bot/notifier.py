from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from telegram.constants import ParseMode
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut

logger = logging.getLogger(__name__)

_MAX_RETRIES = 4


class TelegramNotifier:
    """Send Telegram messages with per-chat rate limiting."""

    def __init__(self, chat_min_interval_sec: float) -> None:
        self._chat_min_interval_sec = chat_min_interval_sec
        self._bot = None
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._last_sent: dict[int, float] = {}

    def bind(self, bot) -> None:
        self._bot = bot

    def _lock(self, chat_id: int) -> asyncio.Lock:
        if chat_id not in self._chat_locks:
            self._chat_locks[chat_id] = asyncio.Lock()
        return self._chat_locks[chat_id]

    async def _throttle(self, chat_id: int) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        last = self._last_sent.get(chat_id, 0.0)
        wait = self._chat_min_interval_sec - (now - last)
        if wait > 0:
            await asyncio.sleep(wait)

    def _mark_sent(self, chat_id: int) -> None:
        self._last_sent[chat_id] = asyncio.get_running_loop().time()

    async def send(self, chat_id: int, text: str, *, parse_mode: str = "HTML") -> bool:
        if self._bot is None:
            logger.warning("Notifier not bound; dropping message to chat %s", chat_id)
            return False
        async with self._lock(chat_id):
            await self._throttle(chat_id)
            try:
                await self._with_retry(
                    lambda: self._bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=parse_mode,
                        disable_web_page_preview=True,
                    )
                )
                return True
            except Exception as exc:
                logger.warning("Telegram send failed: %s", exc)
                return False
            finally:
                self._mark_sent(chat_id)

    async def refresh_price_message(
        self,
        chat_id: int,
        text: str,
        *,
        old_message_id: int | None = None,
    ) -> int | None:
        """Send a new price alert and delete the previous one to avoid chat spam."""
        if self._bot is None:
            return None
        async with self._lock(chat_id):
            await self._throttle(chat_id)
            try:
                message = await self._with_retry(
                    lambda: self._bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                )
            except Exception as exc:
                logger.warning("Telegram price refresh failed: %s", exc)
                self._mark_sent(chat_id)
                return None
            self._mark_sent(chat_id)

            if old_message_id is not None:
                await self._throttle(chat_id)
                await self._delete_plain(chat_id, old_message_id)
                self._mark_sent(chat_id)

            return message.message_id

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        if self._bot is None:
            return
        async with self._lock(chat_id):
            await self._throttle(chat_id)
            await self._delete_plain(chat_id, message_id)
            self._mark_sent(chat_id)

    async def _delete_plain(self, chat_id: int, message_id: int) -> None:
        try:
            await self._with_retry(
                lambda: self._bot.delete_message(chat_id=chat_id, message_id=message_id)
            )
        except Exception:
            logger.debug("Telegram delete failed", exc_info=True)

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
                await asyncio.sleep(min(2**attempt, 8))
        if last_exc:
            raise last_exc
        raise RuntimeError("Telegram request failed")
