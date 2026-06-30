from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from sn_reg_bot.handlers.access_gate import check_access, guarded


def _context(owner_id=1):
    settings = SimpleNamespace(bot_owner_id=owner_id)
    access = MagicMock()
    access.get_user = AsyncMock(return_value=None)
    access.approve_user = AsyncMock()
    return SimpleNamespace(
        application=SimpleNamespace(
            bot_data={
                "settings": settings,
                "access_service": access,
            }
        )
    )


@pytest.mark.asyncio
async def test_check_access_allows_start_without_user_record():
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=99),
        effective_message=MagicMock(text="/start"),
        callback_query=None,
    )
    assert await check_access(update, _context()) is True


@pytest.mark.asyncio
async def test_check_access_allows_owner():
    record = SimpleNamespace(is_approved=lambda: True, access_status=SimpleNamespace(value="approved"))
    ctx = _context(owner_id=42)
    ctx.application.bot_data["access_service"].get_user = AsyncMock(return_value=record)
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=42),
        effective_message=MagicMock(text="/status"),
        callback_query=None,
    )
    assert await check_access(update, ctx) is True


@pytest.mark.asyncio
async def test_guarded_skips_handler_when_denied():
    ctx = _context()
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=99),
        effective_message=AsyncMock(text="/help"),
        callback_query=None,
    )
    update.effective_message.reply_text = AsyncMock()
    handler = AsyncMock()
    wrapped = guarded(handler)
    result = await wrapped(update, ctx)
    assert result is None
    handler.assert_not_called()
    update.effective_message.reply_text.assert_called_once()
