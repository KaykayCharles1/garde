"""Reorg-safe block scanner for EVM chains.

Core idea: a block's hash commits to its entire history. If the last block we processed is
still canonical, nothing before it changed. Each chunk is read like this:

  1. read the headers of our last processed block and of the chunk's end block;
  2. fetch the logs for the range;
  3. read the headers again: the end block must be unchanged and every log's block must
     still be canonical;
  4. only then commit logs and the new checkpoint in ONE database transaction.

If step 1 shows our last block is no longer canonical, we walk back through the stored
checkpoints to the newest one that still is, rewind to it, and rescan. Rescanning is safe
because the hooks are idempotent (dedup is enforced by the database).
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from garde.chains.errors import ChainChangedDuringScan, RpcLagging, RpcRejected, UpstreamError
from garde.chains.scan_rpc import BlockHeader, RawLog, ScanRpc
from garde.db import scan_repo
from garde.sanitize import clean_text

logger = logging.getLogger(__name__)

MAX_REORGS_PER_RUN = 3
CHECKPOINT_CANDIDATES = 2000
CHUNKS_BEFORE_GROWING = 20


@dataclass(frozen=True)
class ScannerConfig:
    confirmations: int = 50  # blocks we wait before trusting a block; a placeholder to tune
    chunk_blocks: int = 10_000
    max_backfill_blocks: int = 500_000
    hash_window_blocks: int = 15_000  # keep checkpoints back past the chain's finality distance
    poll_interval: float = 2.0
    backoff_base: float = 1.0
    backoff_max: float = 60.0


class ScanHooks(Protocol):
    """What M3b/M3c implement. The session is the scanner's transaction: writes made through
    it commit together with the scan checkpoint, or not at all."""

    async def get_filters(self) -> list[dict[str, Any]]:
        """eth_getLogs filters (without the block range). An empty list means nothing to watch."""

    async def on_logs(
        self, session: AsyncSession, logs: list[RawLog], from_block: int, to_block: int
    ) -> None:
        """Called at least once per log. Must be idempotent."""

    async def on_reorg(self, session: AsyncSession, fork_height: int, old_tip: int) -> None:
        """Blocks above fork_height are being rescanned. Reconcile anything built on them."""


@dataclass
class ScanReport:
    initialized_at: int | None = None
    scanned_to: int | None = None
    blocks_scanned: int = 0
    logs_delivered: int = 0
    reorg_fork_height: int | None = None
    gap: tuple[int, int] | None = None


class _RetryChunk(Exception):
    """The node rejected the range; the chunk size was halved. Try again."""


class _BaseNotCanonical(Exception):
    """Our last processed block is no longer on the canonical chain."""


class EvmScanner:
    def __init__(
        self,
        rpc: ScanRpc,
        session_factory: async_sessionmaker[AsyncSession],
        config: ScannerConfig,
        hooks: ScanHooks,
        *,
        chain: str = "robinhood",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._rpc = rpc
        self._session_factory = session_factory
        self._cfg = config
        self._hooks = hooks
        self._chain = chain
        self._sleep = sleep
        self._chunk_blocks = config.chunk_blocks
        self._good_chunks = 0

    @property
    def chunk_blocks(self) -> int:
        return self._chunk_blocks

    # ---- the loop ---------------------------------------------------------------------

    async def run_forever(self) -> None:
        failures = 0
        while True:
            try:
                await self.run_once()
            except UpstreamError as exc:
                failures += 1
                message = clean_text(exc, 200)
                logger.warning(
                    "scan_retry chain=%s failures=%d reason=%s", self._chain, failures, message
                )
                await self._remember_error(message)
                delay = self._backoff(failures)
            except Exception as exc:  # a bug must not silently kill the scanner
                failures += 1
                logger.exception("scan_unexpected_error chain=%s failures=%d", self._chain, 
                                 failures)
                await self._remember_error(f"unexpected {type(exc).__name__}")
                delay = self._backoff(failures)
            else:
                failures = 0
                delay = self._cfg.poll_interval
            await self._sleep(delay)

    def _backoff(self, failures: int) -> float:
        return min(self._cfg.backoff_max, self._cfg.backoff_base * 2 ** (failures - 1))

    async def _remember_error(self, message: str) -> None:
        try:
            async with self._transaction() as session:
                await scan_repo.record_error(session, self._chain, message)
        except Exception:
            logger.exception("scan_state_write_failed chain=%s", self._chain)

    # ---- one pass ---------------------------------------------------------------------

    async def run_once(self) -> ScanReport:
        report = ScanReport()
        head = await self._rpc.block_number()
        target = head - self._cfg.confirmations
        tip = await self._load_tip()

        if tip is None:  # first run: start at the confirmed head, no history
            if target >= 0:
                await self._initialize(head, target)
                report.initialized_at = target
            return report
        if target <= tip.height:
            return report  # nothing newly confirmed (or the node is lagging behind us)

        
        base_height, base_hash = tip.height, tip.block_hash
        if target - base_height > self._cfg.max_backfill_blocks:
            # Downtime longer than the cap. Land HALFWAY inside the cap, not at its edge:
            # if the node keeps failing, the window slides forward with the chain head, and
            # landing at the edge would declare a new gap on every retry.
            skip_to = target - self._cfg.max_backfill_blocks // 2
            report.gap = (base_height + 1, skip_to)
            base_height, base_hash = await self._skip_gap(base_height, skip_to)

        reorgs = 0
        while base_height < target:
            to = min(base_height + self._chunk_blocks, target)
            try:
                delivered, end_hash = await self._scan_chunk(head, base_height, base_hash, to)
            except _RetryChunk:
                continue
            except _BaseNotCanonical:
                reorgs += 1
                if reorgs > MAX_REORGS_PER_RUN:
                    raise ChainChangedDuringScan("the chain kept reorganizing") from None
                report.reorg_fork_height = await self._handle_reorg()
                new_tip = await self._load_tip()
                if new_tip is None:
                    raise UpstreamError("no checkpoint left after a reorg") from None
                base_height, base_hash = new_tip.height, new_tip.block_hash
                continue
            report.blocks_scanned += to - base_height
            report.logs_delivered += delivered
            base_height, base_hash = to, end_hash
        report.scanned_to = base_height
        return report

    async def _scan_chunk(
        self, head: int, base_height: int, base_hash: str, to: int
    ) -> tuple[int, str]:
        start = base_height + 1
        base_header, end_header = await self._headers([base_height, to])
        if base_header.hash != base_hash:
            raise _BaseNotCanonical

        filters = await self._hooks.get_filters()
        logs: list[RawLog] = []
        if filters:
            try:
                logs = await self._fetch_logs(start, to, filters)
            except RpcRejected:
                span = to - start + 1
                if span <= 1:
                    raise
                self._chunk_blocks = max(1, span // 2)  # halve and retry the whole chunk
                self._good_chunks = 0
                raise _RetryChunk from None

        heights = sorted({to} | {log.block_number for log in logs})
        canonical = dict(zip(heights, await self._headers(heights), strict=True))
        if canonical[to].hash != end_header.hash:
            raise ChainChangedDuringScan("the chain changed while a chunk was being scanned")
        for log in logs:
            if canonical[log.block_number].hash != log.block_hash:
                raise ChainChangedDuringScan("a scanned log is no longer on the canonical chain")

        async with self._transaction() as session:
            if logs:
                await self._hooks.on_logs(session, logs, start, to)
            await scan_repo.add_checkpoint(session, self._chain, to, end_header.hash)
            await scan_repo.prune_below(session, self._chain, to - self._cfg.hash_window_blocks)
            await scan_repo.mark_success(session, self._chain, head)

        self._note_chunk_success()
        level = logging.INFO if logs else logging.DEBUG
        logger.log(
            level,
            "scan_chunk chain=%s from=%d to=%d logs=%d chunk=%d",
            self._chain, start, to, len(logs), self._chunk_blocks,
        )
        return len(logs), end_header.hash

    async def _fetch_logs(
        self, start: int, end: int, filters: list[dict[str, Any]]
    ) -> list[RawLog]:
        merged: dict[tuple[str, int], RawLog] = {}
        for log_filter in filters:
            for log in await self._rpc.get_logs(start, end, log_filter):
                if not start <= log.block_number <= end:
                    raise UpstreamError("rpc returned a log outside the requested range")
                merged[(log.block_hash, log.log_index)] = log
        return sorted(merged.values(), key=lambda log: (log.block_number, log.log_index))

    def _note_chunk_success(self) -> None:
        self._good_chunks += 1
        if self._good_chunks >= CHUNKS_BEFORE_GROWING and self._chunk_blocks < self._cfg.chunk_blocks:
            # A shrunken chunk can recover, so one rate-limit blip does not slow us forever.
            self._chunk_blocks = min(self._cfg.chunk_blocks, self._chunk_blocks * 2)
            self._good_chunks = 0

    # ---- start, gap, reorg ------------------------------------------------------------

    async def _initialize(self, head: int, target: int) -> None:
        header = await self._single_header(target)
        async with self._transaction() as session:
            await scan_repo.add_checkpoint(session, self._chain, target, header.hash)
            await scan_repo.mark_success(session, self._chain, head)
        logger.info("scan_initialized chain=%s height=%d", self._chain, target)

    async def _skip_gap(self, old_height: int, skip_to: int) -> tuple[int, str]:
        header = await self._single_header(skip_to)
        async with self._transaction() as session:
            await scan_repo.add_checkpoint(session, self._chain, skip_to, header.hash)
            await scan_repo.record_gap(session, self._chain, old_height + 1, skip_to)
        logger.warning(
            "scan_gap chain=%s first=%d last=%d", self._chain, old_height + 1, skip_to
        )
        return skip_to, header.hash

    async def _handle_reorg(self) -> int:
        async with self._session_factory() as session:
            checkpoints = await scan_repo.load_checkpoints_desc(
                session, self._chain, CHECKPOINT_CANDIDATES
            )
        if not checkpoints:
            raise UpstreamError("no checkpoints to recover from")
        old_tip = checkpoints[0].height

        headers = await self._rpc.get_headers([cp.height for cp in checkpoints])
        fork = next(
            (
                cp
                for cp, header in zip(checkpoints, headers, strict=True)
                if header is not None and header.hash == cp.block_hash
            ),
            None,
        )
        if fork is not None:
            fork_height, fork_hash, deep = fork.height, fork.block_hash, False
        else:
            # Deeper than everything we remember. Restart just below the oldest checkpoint,
            # trusting only a header fetched right now.
            fork_height = max(checkpoints[-1].height - 1, 0)
            fork_hash = (await self._single_header(fork_height)).hash
            deep = True

        async with self._transaction() as session:
            await scan_repo.delete_above(session, self._chain, fork_height)
            if deep:
                await scan_repo.add_checkpoint(session, self._chain, fork_height, fork_hash)
            await self._hooks.on_reorg(session, fork_height, old_tip)
            await scan_repo.record_reorg(
                session, self._chain, rewind=old_tip - fork_height, deep=deep
            )
        logger.warning(
            "scan_reorg chain=%s fork=%d old_tip=%d rewind=%d deep=%s",
            self._chain, fork_height, old_tip, old_tip - fork_height, deep,
        )
        return fork_height

    # ---- small helpers ----------------------------------------------------------------

    async def _headers(self, heights: list[int]) -> list[BlockHeader]:
        headers = await self._rpc.get_headers(heights)
        found = [header for header in headers if header is not None]
        if len(found) != len(headers):
            raise RpcLagging("the node does not have a block it should have")
        return found

    async def _single_header(self, height: int) -> BlockHeader:
        return (await self._headers([height]))[0]

    async def _load_tip(self):
        async with self._session_factory() as session:
            return await scan_repo.load_tip(session, self._chain)

    @asynccontextmanager
    async def _transaction(self) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as session, session.begin():
            yield session