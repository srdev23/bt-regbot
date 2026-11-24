from __future__ import annotations

import logging

from telegram.error import BadRequest, RetryAfter

logger = logging.getLogger(__name__)


async def answer_callback(query, *args, **kwargs) -> None:
    """Answer a callback query, ignoring 'query is too old' / invalid-id errors.

    Telegram invalidates a callback query ~15s after the tap. If the event loop
    was briefly busy, answering can fail with BadRequest; that is harmless and
    must not crash the handler.
    """
    try:
        await query.answer(*args, **kwargs)
    except Exception as exc:
        logger.debug("Ignoring callback answer failure: %s", exc)


def _is_harmless_bad_request(exc: BadRequest) -> bool:
    lowered = str(exc).lower()
    return any(
        token in lowered
        for token in (
            "message is not modified",
            "message to edit not found",
            "message can't be edited",
            "query is too old",
            "query id is invalid",
        )
    )


async def safe_edit_message_text(query, text: str, **kwargs) -> bool:
    """Edit a callback message without crashing on rate limits or stale edits."""
    try:
        await query.edit_message_text(text, **kwargs)
        return True
    except BadRequest as exc:
        if _is_harmless_bad_request(exc):
            logger.debug("Ignoring Telegram edit BadRequest: %s", exc)
            return False
        raise
    except RetryAfter as exc:
        logger.warning("Telegram flood control on edit; retry in %.0fs", exc.retry_after)
        try:
            await query.answer(
                "Telegram rate limit — please wait and try again.",
                show_alert=True,
            )
        except Exception:
            pass
        return False


async def safe_reply_text(message, text: str, **kwargs) -> bool:
    """Reply without crashing on rate limits."""
    try:
        await message.reply_text(text, **kwargs)
        return True
    except RetryAfter as exc:
        logger.warning("Telegram flood control on reply; retry in %.0fs", exc.retry_after)
        return False
