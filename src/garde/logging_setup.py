import logging


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s level=%(levelname)s logger=%(name)s msg=%(message)s",
        force=True,
    )
    # HTTP clients log request URLs, and URLs can carry secrets (the Telegram bot
    # token, explorer API keys). Keep those loggers quiet whatever LOG_LEVEL is.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)