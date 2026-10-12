import logging


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s level=%(levelname)s logger=%(name)s msg=%(message)s",
        force=True,
    )
    # The Telegram HTTP client logs full request URLs at INFO, and those URLs
    # contain the bot token. Keep that logger quiet.
    logging.getLogger("httpx").setLevel(logging.WARNING)