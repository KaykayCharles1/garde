import logging
import sys

import httpx
from telegram.ext import Application, ApplicationBuilder

from garde.bot.handlers import register_handlers
from garde.chains.blockscout import BlockscoutClient
from garde.chains.evm_rpc import Web3Rpc
from garde.chains.robinhood import RobinhoodAdapter
from garde.config import Settings
from garde.logging_setup import setup_logging

logger = logging.getLogger(__name__)


def main() -> None:
    settings = Settings()  # raises a clear validation error if the token is missing
    setup_logging(settings.log_level)

    secret = settings.blockscout_api_key
    if secret is None or not secret.get_secret_value():
        sys.exit("BLOCKSCOUT_API_KEY is required (set it in .env)")

    http_client = httpx.AsyncClient(timeout=15)
    explorer = BlockscoutClient(
        http_client,
        settings.blockscout_api_url,
        settings.robinhood_chain_id,
        secret.get_secret_value(),
    )
    adapter = RobinhoodAdapter(
        explorer,
        Web3Rpc(settings.robinhood_rpc_url),
        explorer_url=settings.robinhood_explorer_url,
        warn_pct=settings.holder_warning_pct,
        critical_pct=settings.holder_critical_pct,
    )

    async def close_resources(_app: Application) -> None:
        await http_client.aclose()

    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token.get_secret_value())
        .post_shutdown(close_resources)
        .build()
    )
    app.bot_data["adapters"] = {adapter.chain: adapter}
    register_handlers(app)
    logger.info("garde_starting")
    app.run_polling()


if __name__ == "__main__":
    main()