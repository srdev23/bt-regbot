from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from telegram.constants import ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut

logger = logging.getLogger(__name__)

_MAX_RETRIES = 4
_MAX_RETRY_AFTER_SEC = 60.0


class TelegramRateLimitedError(Exception):
    """Raised when Telegram flood control requires waiting longer than we allow."""


class TelegramNotifier:
    """Send Telegram messages with per-chat rate limiting."""

    def __init__(self, chat_min_interval_sec: float) -> None:
        self._chat_min_interval_sec = chat_min_interval_sec
        self._bot = None
        self._chat_locks: dict[int, asyncio.Lock] = {}
        self._last_sent: dict[int, float] = {}
        self._chat_backoff_until: dict[int, float] = {}
        self._background_tasks: set[asyncio.Task] = set()

    def bind(self, bot) -> None:
        self._bot = bot

    def chat_in_backoff(self, chat_id: int) -> bool:
        return self._in_backoff(chat_id)

    def _lock(self, chat_id: int) -> asyncio.Lock:
        if chat_id not in self._chat_locks:
            self._chat_locks[chat_id] = asyncio.Lock()
        return self._chat_locks[chat_id]

    def _spawn(self, coro, *, name: str) -> None:
        """Schedule background work and retain a strong reference until done."""
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    def _in_backoff(self, chat_id: int) -> bool:
        until = self._chat_backoff_until.get(chat_id, 0.0)
        return asyncio.get_running_loop().time() < until

    def _set_backoff(self, chat_id: int, retry_after_sec: float) -> None:
        loop = asyncio.get_running_loop()
        until = loop.time() + min(retry_after_sec, 3600.0)
        current = self._chat_backoff_until.get(chat_id, 0.0)
        self._chat_backoff_until[chat_id] = max(current, until)

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
        if self._in_backoff(chat_id):
            logger.debug("Skipping Telegram send chat=%s (flood-control backoff)", chat_id)
            return False
        async with self._lock(chat_id):
            if self._in_backoff(chat_id):
                logger.debug("Skipping Telegram send chat=%s (flood-control backoff)", chat_id)
                return False
            await self._throttle(chat_id)
            try:
                await self._with_retry(
                    lambda: self._bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=parse_mode,
                        disable_web_page_preview=True,
                    ),
                    chat_id=chat_id,
                )
                return True
            except TelegramRateLimitedError:
                return False
            except Exception as exc:
                logger.warning("Telegram send failed: %s", exc)
                return False
            finally:
                self._mark_sent(chat_id)

    def send_later(self, chat_id: int, text: str, *, parse_mode: str = "HTML") -> None:
        """Fire-and-forget Telegram message — never blocks registration."""
        if self._bot is None:
            return
        self._spawn(
            self.send(chat_id, text, parse_mode=parse_mode),
            name=f"notify-{chat_id}",
        )

    def delete_later(self, chat_id: int, message_id: int) -> None:
        """Fire-and-forget message deletion — never blocks registration."""
        if self._bot is None:
            return
        self._spawn(
            self.delete_message(chat_id, message_id),
            name=f"delete-{chat_id}-{message_id}",
        )

    async def replace_price_message(
        self,
        chat_id: int,
        text: str,
        *,
        old_message_id: int | None = None,
    ) -> int | None:
        """Delete the previous alert and post a new one so Telegram notifies the user."""
        if self._bot is None:
            return None
        if self._in_backoff(chat_id):
            if old_message_id is not None:
                edited = await self._edit_price_message(chat_id, text, old_message_id)
                if edited is not None:
                    return edited
            logger.debug("Skipping price alert chat=%s (flood-control backoff)", chat_id)
            return None
        async with self._lock(chat_id):
            if self._in_backoff(chat_id):
                if old_message_id is not None:
                    edited = await self._edit_price_message(chat_id, text, old_message_id)
                    if edited is not None:
                        return edited
                logger.debug("Skipping price alert chat=%s (flood-control backoff)", chat_id)
                return None
            if old_message_id is not None:
                await self._throttle(chat_id)
                await self._delete_plain(chat_id, old_message_id)
                self._mark_sent(chat_id)

            await self._throttle(chat_id)
            try:
                message = await self._with_retry(
                    lambda: self._bot.send_message(
                        chat_id=chat_id,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    ),
                    chat_id=chat_id,
                )
            except TelegramRateLimitedError:
                if old_message_id is not None:
                    return await self._edit_price_message(chat_id, text, old_message_id)
                return None
            except Exception as exc:
                logger.warning("Telegram price alert send failed: %s", exc)
                self._mark_sent(chat_id)
                if old_message_id is not None:
                    return await self._edit_price_message(chat_id, text, old_message_id)
                return None
            self._mark_sent(chat_id)
            return message.message_id

    async def _edit_price_message(
        self, chat_id: int, text: str, message_id: int
    ) -> int | None:
        """Silent in-place update when delete+send is blocked by flood control."""
        if self._bot is None:
            return None
        try:
            await self._with_retry(
                lambda: self._bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                ),
                chat_id=chat_id,
            )
            return message_id
        except BadRequest as exc:
            if "message is not modified" in str(exc).lower():
                return message_id
            logger.debug("Price alert edit failed chat=%s msg=%s: %s", chat_id, message_id, exc)
            return None
        except TelegramRateLimitedError:
            return None
        except Exception as exc:
            logger.debug("Price alert edit failed chat=%s msg=%s: %s", chat_id, message_id, exc)
            return None

    async def refresh_price_message(
        self,
        chat_id: int,
        text: str,
        *,
        old_message_id: int | None = None,
    ) -> int | None:
        """Backward-compatible alias — always delete old alert and send a new one."""
        return await self.replace_price_message(
            chat_id, text, old_message_id=old_message_id
        )

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        if self._bot is None:
            return False
        if self._in_backoff(chat_id):
            return False
        async with self._lock(chat_id):
            if self._in_backoff(chat_id):
                return False
            await self._throttle(chat_id)
            deleted = await self._delete_plain(chat_id, message_id)
            self._mark_sent(chat_id)
            return deleted

    async def _delete_plain(self, chat_id: int, message_id: int) -> bool:
        try:
            await self._with_retry(
                lambda: self._bot.delete_message(chat_id=chat_id, message_id=message_id),
                chat_id=chat_id,
            )
            return True
        except TelegramRateLimitedError:
            return False
        except BadRequest as exc:
            lowered = str(exc).lower()
            if any(
                token in lowered
                for token in (
                    "message to delete not found",
                    "message can't be deleted",
                    "message identifier is not specified",
                )
            ):
                return True
            logger.warning(
                "Telegram delete failed chat=%s msg=%s: %s",
                chat_id,
                message_id,
                exc,
            )
            return False
        except Exception as exc:
            logger.warning(
                "Telegram delete failed chat=%s msg=%s: %s",
                chat_id,
                message_id,
                exc,
            )
            return False

    async def _with_retry(
        self,
        call: Callable[[], Awaitable[Any]],
        *,
        chat_id: int | None = None,
    ):
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES):
            try:
                return await call()
            except RetryAfter as exc:
                retry_after = float(exc.retry_after)
                if retry_after > _MAX_RETRY_AFTER_SEC:
                    if chat_id is not None:
                        self._set_backoff(chat_id, retry_after)
                    logger.warning(
                        "Telegram flood control chat=%s retry_after=%.0fs; "
                        "skipping (will retry after backoff)",
                        chat_id,
                        retry_after,
                    )
                    raise TelegramRateLimitedError(str(exc)) from exc
                wait = retry_after + 1.0
                logger.warning("Telegram rate limit, waiting %.0fs", wait)
                await asyncio.sleep(wait)
                last_exc = exc
            except BadRequest:
                raise
            except (TimedOut, NetworkError) as exc:
                last_exc = exc
                if attempt + 1 >= _MAX_RETRIES:
                    break
                await asyncio.sleep(min(2**attempt, 8))
        if last_exc:
            raise last_exc
        raise RuntimeError("Telegram request failed")
