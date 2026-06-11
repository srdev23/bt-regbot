#!/usr/bin/env python3
from __future__ import annotations

import logging
import sys

from telegram import Update

from regbot.bot_app import build_application
from regbot.config import Settings


def main() -> None:
    logging.basicConfig(
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        level=logging.INFO,
    )
    try:
        settings = Settings.load()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        sys.exit(1)

    app = build_application(settings)
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
