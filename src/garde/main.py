import asyncio
import logging
import sys

import httpx
from telegram.ext import Application, ApplicationBuilder

from garde.bot.handlers import register_handlers
from garde.chains.blockscout import BlockscoutClient
from garde.chains.evm_rpc import Web3Rpc
from garde.chains.evm_scanner import EvmScanner, ScannerConfig
from garde.chains.robinhood import RobinhoodAdapter
from garde.chains.scan_rpc import HttpScanRpc
from garde.config import Settings
from garde.db.session import create_engine, create_session_factory
from garde.delivery.sender import DeliverySender, TelegramMessenger
from garde.domain import Chain
from garde.events.decoders import Thresholds
from garde.events.hooks import EventHooks
from garde.logging_setup import setup_logging
from garde.runtime import schema_is_current, start_background_tasks, stop_background_tasks
from garde.watch_service import ServiceConfig, WatchService

logger = logging.getLogger(__name__)


def main() -> None:
    settings = Settings()  # raises a clear validation error if the token is missing
    setup_logging(settings.log_level)

    secret = settings.blockscout_api_key
    if secret is None or not secret.get_secret_value():
        sys.exit("BLOCKSCOUT_API_KEY is required (set it in .env)")
    if not asyncio.run(schema_is_current(settings.database_url)):
        sys.exit("The database is not up to date. Run: alembic upgrade head")

    engine = create_engine(settings.database_url)
    session_factory = create_session_factory(engine)

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
    scan_rpc = HttpScanRpc(http_client, settings.robinhood_rpc_url)
    watch_service = WatchService(
        session_factory,
        {adapter.chain: adapter},
        {Chain.ROBINHOOD: scan_rpc.block_number},
        ServiceConfig(
            max_per_chat=settings.max_tokens_per_chat,
            max_global=settings.max_tokens_global,
            confirmations=settings.scan_confirmations,
            large_transfer_pct=settings.large_transfer_warning_pct,
        ),
    )
    hooks = EventHooks(
        session_factory,
        chain=Chain.ROBINHOOD,
        explorer_url=settings.robinhood_explorer_url,
        thresholds=Thresholds(
            warning_pct=settings.large_transfer_warning_pct,
            critical_pct=settings.large_transfer_critical_pct,
        ),
    )
    scanner = EvmScanner(
        scan_rpc,
        session_factory,
        ScannerConfig(
            confirmations=settings.scan_confirmations,
            chunk_blocks=settings.scan_chunk_blocks,
            max_backfill_blocks=settings.scan_max_backfill_blocks,
            hash_window_blocks=settings.scan_hash_window_blocks,
            poll_interval=settings.scan_poll_seconds,
            backoff_base=settings.scan_backoff_base_seconds,
            backoff_max=settings.scan_backoff_max_seconds,
        ),
        hooks,
    )

    async def start_background(app: Application) -> None:
        sender = DeliverySender(session_factory, TelegramMessenger(app.bot))
        await start_background_tasks(
            app, {"scanner": scanner.run_forever, "sender": sender.run_forever}
        )

    async def close_resources(_app: Application) -> None:
        await http_client.aclose()
        await engine.dispose()

    app = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token.get_secret_value())
        .post_init(start_background)
        .post_stop(stop_background_tasks)
        .post_shutdown(close_resources)
        .build()
    )
    app.bot_data["adapters"] = {adapter.chain: adapter}
    app.bot_data["watch_service"] = watch_service
    register_handlers(app)
    logger.info("garde_starting")
    app.run_polling()


if __name__ == "__main__":
    main()