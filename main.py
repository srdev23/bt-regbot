from __future__ import annotations

import logging

from sn_reg_bot.bot_app import build_application
from sn_reg_bot.config import Settings
from sn_reg_bot.logging_config import setup_logging

logger = logging.getLogger(__name__)


def main() -> None:
    settings = Settings.load()
    setup_logging(settings.data_dir / "logs", settings.log_level)
    if settings.bot_owner_id is not None:
        logger.info("Bot owner id configured for admin /status")
    app = build_application(settings)
    logger.info("Starting SN Registration Bot")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
