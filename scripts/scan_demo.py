"""Run the real scanner against the live chain on a throwaway database.

    python scripts/scan_demo.py [seconds] [extra token address ...]

Run it twice, a minute apart: the second run backfills the downtime in chunks.
"""

import asyncio
import sys

import httpx

from garde.chains.addresses import normalize_evm_address
from garde.chains.evm_scanner import EvmScanner, ScannerConfig
from garde.chains.scan_rpc import HttpScanRpc, RawLog
from garde.config import Settings
from garde.db import models  # noqa: F401
from garde.db.base import Base
from garde.db.models import ProcessedBlock, ScanState
from garde.db.session import create_engine, create_session_factory
from garde.logging_setup import setup_logging

DEMO_DB = "sqlite+aiosqlite:///./scan_demo.db"
DEFAULT_WATCH = (
    "0xe4d5b1f1a64099c7c38d003ad93196258ae57a03",
    "0x9e00366e55e10e71e4576abafefe3c847cfb6754",
)


class DemoHooks:
    def __init__(self, addresses: list[str]) -> None:
        self._filters = [{"address": addresses}]

    async def get_filters(self) -> list[dict]:
        return self._filters

    async def on_logs(self, session, logs: list[RawLog], from_block: int, to_block: int) -> None:
        for log in logs:
            topic = log.topics[0][:10] if log.topics else "none"
            print(f"LOG block={log.block_number} address={log.address} topic0={topic}...")

    async def on_reorg(self, session, fork_height: int, old_tip: int) -> None:
        print(f"REORG: rewound from {old_tip} to {fork_height}")


async def main(seconds: float, extra: list[str]) -> None:
    settings = Settings()
    setup_logging("INFO")
    addresses = list(DEFAULT_WATCH) + [normalize_evm_address(a) for a in extra]

    engine = create_engine(DEMO_DB)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    config = ScannerConfig(
        confirmations=settings.scan_confirmations,
        chunk_blocks=settings.scan_chunk_blocks,
        max_backfill_blocks=settings.scan_max_backfill_blocks,
        hash_window_blocks=settings.scan_hash_window_blocks,
        poll_interval=settings.scan_poll_seconds,
        backoff_base=settings.scan_backoff_base_seconds,
        backoff_max=settings.scan_backoff_max_seconds,
    )

    async with httpx.AsyncClient(timeout=20) as http:
        scanner = EvmScanner(
            HttpScanRpc(http, settings.robinhood_rpc_url), factory, config, DemoHooks(addresses)
        )
        try:
            await asyncio.wait_for(scanner.run_forever(), timeout=seconds)
        except TimeoutError:
            pass

    async with factory() as session:
        state = await session.get(ScanState, "robinhood")
        checkpoints = (await session.execute(ProcessedBlock.__table__.select())).all()
    print(f"\nstopped. checkpoints stored: {len(checkpoints)}")
    if state:
        print(
            f"last_head={state.last_head} reorgs={state.reorgs_total} "
            f"gaps={state.gaps_total} last_error={state.last_error}"
        )
    await engine.dispose()


if __name__ == "__main__":
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    asyncio.run(main(duration, sys.argv[2:]))