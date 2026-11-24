from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable, TypeVar

from telegram import Bot, Message

T = TypeVar("T")


@asynccontextmanager
async def progress_message(
    message: Message,
    text: str = "⏳ Just a sec, working...",
) -> AsyncIterator[Message]:
    """Reply with a temporary status message and delete it when done."""
    progress: Message | None = None
    try:
        progress = await message.reply_text(text)
        yield progress
    finally:
        if progress is not None:
            try:
                await progress.delete()
            except Exception:
                pass


@asynccontextmanager
async def transient_status(
    bot: Bot,
    chat_id: int,
    text: str = "⏳ Just a sec, working...",
) -> AsyncIterator[None]:
    """Send a standalone temporary status message (e.g. after deleting user input)."""
    progress: Message | None = None
    try:
        progress = await bot.send_message(chat_id=chat_id, text=text)
        yield
    finally:
        if progress is not None:
            try:
                await progress.delete()
            except Exception:
                pass


async def run_with_progress(
    message: Message,
    fn: Callable[[], Awaitable[T]],
    *,
    text: str = "⏳ Just a sec, working...",
) -> T:
    async with progress_message(message, text):
        return await fn()


async def run_blocking_with_progress(
    message: Message,
    fn: Callable[[], T],
    *,
    text: str = "⏳ Just a sec, working...",
) -> T:
    async with progress_message(message, text):
        return await asyncio.to_thread(fn)


async def run_blocking_with_transient_status(
    bot: Bot,
    chat_id: int,
    fn: Callable[[], T],
    *,
    text: str = "⏳ Just a sec, working...",
) -> T:
    async with transient_status(bot, chat_id, text):
        return await asyncio.to_thread(fn)
