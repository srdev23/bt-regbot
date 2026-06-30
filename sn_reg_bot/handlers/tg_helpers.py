from __future__ import annotations

import logging

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
