import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import BadRequest, RetryAfter

from sn_reg_bot.notifier import TelegramNotifier


@pytest.fixture
def notifier():
    n = TelegramNotifier(chat_min_interval_sec=0.0)
    bot = MagicMock()
    bot.send_message = AsyncMock()
    bot.edit_message_text = AsyncMock()
    bot.delete_message = AsyncMock()
    n.bind(bot)
    return n, bot


@pytest.mark.asyncio
async def test_replace_price_message_posts_without_old_id(notifier):
    n, bot = notifier
    bot.send_message.return_value = MagicMock(message_id=99)

    msg_id = await n.replace_price_message(100, "<b>price</b>")

    assert msg_id == 99
    bot.send_message.assert_awaited_once()
    bot.delete_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_replace_price_message_deletes_then_sends(notifier):
    n, bot = notifier
    bot.send_message.return_value = MagicMock(message_id=99)

    msg_id = await n.replace_price_message(
        100, "<b>price</b>", old_message_id=42
    )

    assert msg_id == 99
    bot.delete_message.assert_awaited_once_with(chat_id=100, message_id=42)
    bot.send_message.assert_awaited_once()
    bot.edit_message_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_replace_price_message_still_sends_when_delete_fails(notifier):
    n, bot = notifier
    bot.delete_message.side_effect = BadRequest("message to delete not found")
    bot.send_message.return_value = MagicMock(message_id=99)

    msg_id = await n.replace_price_message(
        100, "<b>price</b>", old_message_id=42
    )

    assert msg_id == 99
    bot.delete_message.assert_awaited_once()
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_refresh_price_message_delegates_to_replace(notifier):
    n, bot = notifier
    bot.send_message.return_value = MagicMock(message_id=99)

    msg_id = await n.refresh_price_message(
        100, "<b>price</b>", old_message_id=42
    )

    assert msg_id == 99
    bot.delete_message.assert_awaited_once()
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_later_retains_task_until_complete(notifier):
    n, bot = notifier
    deleted = asyncio.Event()

    async def slow_delete(**_kwargs):
        await asyncio.sleep(0.05)
        deleted.set()

    bot.delete_message.side_effect = slow_delete

    n.delete_later(100, 7)
    assert len(n._background_tasks) == 1
    await asyncio.sleep(0.15)
    assert deleted.is_set()
    assert len(n._background_tasks) == 0


@pytest.mark.asyncio
async def test_delete_message_treats_not_found_as_success(notifier):
    n, bot = notifier
    bot.delete_message.side_effect = BadRequest("message to delete not found")

    ok = await n.delete_message(100, 42)

    assert ok is True


@pytest.mark.asyncio
async def test_send_skips_during_flood_backoff(notifier):
    n, bot = notifier
    n._set_backoff(100, 3600.0)

    ok = await n.send(100, "hello")

    assert ok is False
    bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_retry_after_cap_sets_backoff_and_fails_fast(notifier):
    n, bot = notifier
    bot.send_message.side_effect = RetryAfter(retry_after=5000)

    ok = await n.send(100, "hello")

    assert ok is False
    assert n._in_backoff(100)
    bot.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_short_retry_after_waits_and_retries(notifier):
    n, bot = notifier
    bot.send_message.side_effect = [
        RetryAfter(retry_after=1),
        MagicMock(message_id=1),
    ]

    ok = await n.send(100, "hello")

    assert ok is True
    assert bot.send_message.await_count == 2


@pytest.mark.asyncio
async def test_replace_price_message_edits_when_backoff(notifier):
    n, bot = notifier
    n._set_backoff(100, 3600.0)
    bot.edit_message_text = AsyncMock()

    msg_id = await n.replace_price_message(100, "<b>price</b>", old_message_id=42)

    assert msg_id == 42
    bot.edit_message_text.assert_awaited_once()
    bot.send_message.assert_not_awaited()
