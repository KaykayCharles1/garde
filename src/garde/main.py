import logging

from telegram.ext import ApplicationBuilder

from garde.bot.handlers import register_handlers
from garde.config import Settings
from garde.logging_setup import setup_logging

logger = logging.getLogger(__name__)


def main() -> None:
    settings = Settings()  # raises a clear validation error if the token is missing
    setup_logging(settings.log_level)
    app = ApplicationBuilder().token(settings.telegram_bot_token.get_secret_value()).build()
    register_handlers(app)
    logger.info("garde_starting")
    app.run_polling()


if __name__ == "__main__":
    main()