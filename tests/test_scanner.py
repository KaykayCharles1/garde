from dataclasses import replace

import pytest

from garde.chains.errors import ChainChangedDuringScan, RpcLagging, RpcRateLimited, UpstreamError
from garde.chains.evm_scanner import EvmScanner, ScannerConfig
from garde.chains.scan_rpc import RawLog
from garde.db import scan_repo
from garde.db.models import ScanState
from tests.sim_chain import SimChain, make_hash

WATCHED = "0x" + "aa" * 20
OTHER = "0x" + "bb" * 20
BASE_CONFIG = ScannerConfig(
    confirmations=5,
    chunk_blocks=100,
    max_backfill_blocks=10_000,
    hash_window_blocks=5_000,
    poll_interval=3.0,
    backoff_base=1.0,
    backoff_max=8.0,
)


class Stop(Exception):
    """Raised by a fake sleep to end run_forever in tests."""


class RecordingHooks:
    def __init__(self) -> None:
        self.filters: list[dict] = [{"address": [WATCHED]}]
        self.log_calls: list[tuple[int, int, list[RawLog]]] = []
        self.reorgs: list[tuple[int, int]] = []
        self.fail_next_on_logs: Exception | None = None

    async def get_filters(self) -> list[dict]:
        return self.filters

    async def on_logs(self, session, logs, from_block, to_block) -> None:
        if self.fail_next_on_logs is not None:
            error, self.fail_next_on_logs = self.fail_next_on_logs, None
            raise error
        self.log_calls.append((from_block, to_block, list(logs)))

    async def on_reorg(self, session, fork_height, old_tip) -> None:
        self.reorgs.append((fork_height, old_tip))


def make_scanner(chain, factory, hooks=None, *, sleep=None, **overrides):
    hooks = hooks or RecordingHooks()
    kwargs = {} if sleep is None else {"sleep": sleep}
    config = replace(BASE_CONFIG, **overrides)
    return EvmScanner(chain, factory, config, hooks, **kwargs), hooks


async def tip_height(factory):
    async with factory() as session:
        tip = await scan_repo.load_tip(session, "robinhood")
        return tip.height if tip else None


async def checkpoint_heights(factory):
    async with factory() as session:
        rows = await scan_repo.load_checkpoints_desc(session, "robinhood", 10_000)
        return sorted(row.height for row in rows)


async def read_state(factory):
    async with factory() as session:
        return await session.get(ScanState, "robinhood")


def delivered_heights(hooks):
    return [log.block_number for _, _, logs in hooks.log_calls for log in logs]


def started_chain(length: int = 1000) -> SimChain:
    chain = SimChain()
    chain.mine(length)
    return chain


# --- start, advance, confirmation lag ------------------------------------------------------


async def test_fresh_start_begins_at_the_confirmed_head_without_history(session_factory):
    chain = started_chain()
    chain.add_log(500, WATCHED)  # history from before we started: must NOT be delivered
    scanner, hooks = make_scanner(chain, session_factory)
    report = await scanner.run_once()
    assert report.initialized_at == 995
    assert hooks.log_calls == []
    assert await tip_height(session_factory) == 995


async def test_new_logs_are_delivered_once_and_only_when_confirmed(session_factory):
    chain = started_chain()
    scanner, hooks = make_scanner(chain, session_factory)
    await scanner.run_once()  # initialize at 995

    chain.mine(50)  # head 1050, confirmed up to 1045
    for height in (1000, 1040, 1048):  # 1048 is inside the confirmation lag
        chain.add_log(height, WATCHED)
    await scanner.run_once()
    assert delivered_heights(hooks) == [1000, 1040]

    calls_before = len(hooks.log_calls)
    await scanner.run_once()  # nothing new: no duplicate delivery
    assert len(hooks.log_calls) == calls_before

    chain.mine(10)  # head 1060, confirmed up to 1055: now 1048 qualifies
    await scanner.run_once()
    assert delivered_heights(hooks) == [1000, 1040, 1048]


async def test_chunks_are_contiguous_and_cover_the_whole_range(session_factory):
    chain = started_chain()
    scanner, hooks = make_scanner(chain, session_factory)
    await scanner.run_once()
    chain.mine(1000)  # head 2000, confirmed up to 1995
    for height in range(1000, 2000, 100):  # one log per chunk so every chunk calls the hook
        chain.add_log(height, WATCHED)
    await scanner.run_once()
    ranges = [(start, end) for start, end, _ in hooks.log_calls]
    assert ranges == [(996 + 100 * i, 1095 + 100 * i) for i in range(10)]
    assert await checkpoint_heights(session_factory) == [995] + [1095 + 100 * i for i in range(10)]


async def test_no_filters_advances_without_asking_for_logs(session_factory):
    chain = started_chain()
    scanner, hooks = make_scanner(chain, session_factory)
    hooks.filters = []
    await scanner.run_once()
    chain.mine(50)
    await scanner.run_once()
    assert await tip_height(session_factory) == 1045
    assert chain.calls["get_logs"] == 0
    assert hooks.log_calls == []


async def test_multiple_filters_are_merged_deduplicated_and_ordered(session_factory):
    chain = started_chain()
    scanner, hooks = make_scanner(chain, session_factory)
    hooks.filters = [{"address": [WATCHED]}, {"address": [WATCHED, OTHER]}]
    await scanner.run_once()
    chain.mine(50)
    chain.add_log(1010, WATCHED, log_index=0)
    chain.add_log(1005, OTHER, log_index=1)
    await scanner.run_once()
    assert delivered_heights(hooks) == [1005, 1010]
    assert len(hooks.log_calls[0][2]) == 2


async def test_old_checkpoints_are_pruned(session_factory):
    chain = started_chain()
    scanner, _ = make_scanner(chain, session_factory, hash_window_blocks=100)
    await scanner.run_once()
    for _ in range(10):
        chain.mine(10)
        await scanner.run_once()
    heights = await checkpoint_heights(session_factory)
    tip = await tip_height(session_factory)
    assert min(heights) >= tip - 100
    assert tip in heights


# --- reorgs ----------------------------------------------------------------------------


async def test_reorg_rewinds_to_the_nearest_canonical_checkpoint_and_rescans(session_factory):
    chain = started_chain()
    scanner, hooks = make_scanner(chain, session_factory)
    await scanner.run_once()  # checkpoint 995
    for step in range(5):  # checkpoints at 1005, 1015, 1025, 1035, 1045
        chain.mine(10)
        if step == 4:
            chain.add_log(1044, WATCHED)
        await scanner.run_once()
    assert delivered_heights(hooks) == [1044]

    chain.reorg(fork_height=1042, new_blocks=20)  # block 1044 no longer exists
    chain.add_log(1050, WATCHED)
    hooks.log_calls.clear()
    await scanner.run_once()

    assert hooks.reorgs == [(1035, 1045)]
    assert delivered_heights(hooks) == [1050]
    for _, _, logs in hooks.log_calls:
        for log in logs:
            assert log.block_hash == chain.hash_at(log.block_number)
    state = await read_state(session_factory)
    assert state.reorgs_total == 1
    assert state.deep_reorgs_total == 0
    assert state.last_reorg_rewind_blocks == 10
    assert await tip_height(session_factory) == 1057


async def test_reorg_deeper_than_every_checkpoint_restarts_below_the_oldest(session_factory):
    chain = started_chain()
    scanner, hooks = make_scanner(chain, session_factory, hash_window_blocks=30)
    await scanner.run_once()
    for _ in range(5):
        chain.mine(10)
        await scanner.run_once()
    assert await checkpoint_heights(session_factory) == [1015, 1025, 1035, 1045]

    chain.reorg(fork_height=500, new_blocks=600)  # head 1100
    await scanner.run_once()

    assert hooks.reorgs == [(1014, 1045)]
    state = await read_state(session_factory)
    assert state.deep_reorgs_total == 1
    assert await tip_height(session_factory) == 1095


async def test_reorg_during_a_scan_commits_nothing_and_recovers(session_factory):
    chain = started_chain()
    scanner, hooks = make_scanner(chain, session_factory)
    await scanner.run_once()
    chain.mine(50)
    chain.on_get_logs = lambda: chain.reorg(fork_height=1040, new_blocks=20)

    with pytest.raises(ChainChangedDuringScan):
        await scanner.run_once()
    assert await tip_height(session_factory) == 995
    assert hooks.log_calls == []

    await scanner.run_once()  # the chain is stable now
    assert await tip_height(session_factory) == 1055


# --- crashes and failures ---------------------------------------------------------------


async def test_a_failing_hook_rolls_back_and_the_logs_are_redelivered(session_factory):
    chain = started_chain()
    scanner, hooks = make_scanner(chain, session_factory)
    await scanner.run_once()
    chain.mine(50)
    chain.add_log(1000, WATCHED)
    hooks.fail_next_on_logs = RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await scanner.run_once()
    assert await tip_height(session_factory) == 995  # checkpoint did not advance

    await scanner.run_once()
    assert delivered_heights(hooks) == [1000]  # at-least-once: delivered on the retry
    assert await tip_height(session_factory) == 1045


async def test_backoff_doubles_is_capped_and_resets_after_success(session_factory):
    chain = started_chain()
    chain.fail_on["block_number"] = [UpstreamError("down")] * 5
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) >= 7:
            raise Stop

    scanner, _ = make_scanner(chain, session_factory, sleep=fake_sleep)
    with pytest.raises(Stop):
        await scanner.run_forever()
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 8.0, 3.0, 3.0]
    state = await read_state(session_factory)
    assert state.last_error is None  # cleared by the later success


async def test_failures_are_recorded_for_status(session_factory):
    chain = started_chain()
    chain.fail_on["block_number"] = [UpstreamError("rpc down")]

    async def stop_at_once(delay: float) -> None:
        raise Stop

    scanner, _ = make_scanner(chain, session_factory, sleep=stop_at_once)
    with pytest.raises(Stop):
        await scanner.run_forever()
    assert (await read_state(session_factory)).last_error == "rpc down"


async def test_a_node_that_is_behind_us_is_not_a_reorg(session_factory):
    chain = started_chain()
    scanner, hooks = make_scanner(chain, session_factory)
    await scanner.run_once()
    chain.mine(50)
    await scanner.run_once()  # tip 1045
    chain.head_override = 1000  # the node we asked is lagging
    report = await scanner.run_once()
    assert report.scanned_to is None
    assert hooks.reorgs == []
    assert await tip_height(session_factory) == 1045


async def test_a_missing_block_is_a_lagging_node_not_a_reorg(session_factory):
    chain = started_chain()
    scanner, hooks = make_scanner(chain, session_factory)
    await scanner.run_once()
    chain.mine(50)
    chain.hidden = {1045}
    with pytest.raises(RpcLagging):
        await scanner.run_once()
    assert hooks.reorgs == []
    assert await tip_height(session_factory) == 995


async def test_a_log_outside_the_requested_range_is_rejected(session_factory):
    chain = started_chain()
    scanner, hooks = make_scanner(chain, session_factory)
    await scanner.run_once()
    chain.mine(50)
    chain.injected = [
        RawLog(WATCHED, (), "0x", 5, make_hash("x"), make_hash("y"), 0, 0)
    ]
    with pytest.raises(UpstreamError, match="outside"):
        await scanner.run_once()
    assert await tip_height(session_factory) == 995
    assert hooks.log_calls == []


# --- chunk sizing and backfill ----------------------------------------------------------


async def test_oversized_ranges_are_halved_until_the_node_accepts_them(session_factory):
    chain = started_chain()
    scanner, hooks = make_scanner(chain, session_factory, chunk_blocks=1000)
    await scanner.run_once()
    chain.mine(1000)
    chain.max_span = 50
    for height in (1000, 1500, 1990):
        chain.add_log(height, WATCHED)
    await scanner.run_once()
    assert delivered_heights(hooks) == [1000, 1500, 1990]  # every log, exactly once
    assert scanner.chunk_blocks < 1000


async def test_a_shrunken_chunk_grows_back_when_the_node_is_healthy(session_factory):
    chain = started_chain()
    scanner, _ = make_scanner(
        chain, session_factory, chunk_blocks=1000, max_backfill_blocks=100_000
    )
    await scanner.run_once()
    chain.mine(1000)
    chain.max_span = 50
    await scanner.run_once()
    small = scanner.chunk_blocks
    assert small <= 62

    chain.max_span = None
    chain.mine(20_000)
    await scanner.run_once()
    assert scanner.chunk_blocks > small


async def test_downtime_beyond_the_cap_lands_halfway_inside_it_and_records_the_gap(
    session_factory,
):
    chain = started_chain()
    scanner, hooks = make_scanner(
        chain, session_factory, max_backfill_blocks=2000, chunk_blocks=500
    )
    await scanner.run_once()  # tip 995
    chain.mine(9000)  # head 10000, confirmed up to 9995
    chain.add_log(2000, WATCHED)  # inside the skipped gap
    chain.add_log(9000, WATCHED)  # inside the replayed window (8996..9995)

    report = await scanner.run_once()

    assert report.gap == (996, 8995)
    assert delivered_heights(hooks) == [9000]
    state = await read_state(session_factory)
    assert (state.gaps_total, state.last_gap_first, state.last_gap_last) == (1, 996, 8995)
    assert state.last_head == 10_000


async def test_exactly_at_the_cap_is_not_a_gap(session_factory):
    chain = started_chain()
    scanner, _ = make_scanner(
        chain, session_factory, max_backfill_blocks=2000, chunk_blocks=500
    )
    await scanner.run_once()  # tip 995
    chain.mine(2000)  # head 3000, confirmed up to 2995: exactly 2000 behind
    report = await scanner.run_once()
    assert report.gap is None
    assert await tip_height(session_factory) == 2995


async def test_a_failed_pass_after_a_gap_does_not_declare_another_gap(session_factory):
    chain = started_chain()
    scanner, _ = make_scanner(
        chain, session_factory, max_backfill_blocks=2000, chunk_blocks=500
    )
    await scanner.run_once()
    chain.mine(9000)
    chain.fail_on["get_logs"] = [UpstreamError("rate limited")]
    with pytest.raises(UpstreamError):
        await scanner.run_once()  # gap declared, then the first chunk fails

    chain.mine(30)  # the head moves on while the node is failing
    await scanner.run_once()  # retry: must NOT skip again

    state = await read_state(session_factory)
    assert state.gaps_total == 1
    assert await tip_height(session_factory) == 10_025


class CuttingHooks(RecordingHooks):
    """Asks the scanner to end the first chunk at a given block, once."""

    def __init__(self, cut: int) -> None:
        super().__init__()
        self.cut: int | None = cut

    async def on_logs(self, session, logs, from_block, to_block):
        await super().on_logs(session, logs, from_block, to_block)
        cut, self.cut = self.cut, None
        return cut


class AlwaysCutAtStart(RecordingHooks):
    async def on_logs(self, session, logs, from_block, to_block):
        await super().on_logs(session, logs, from_block, to_block)
        return from_block - 1


async def test_a_rate_limited_get_logs_shrinks_the_next_chunk_and_backs_off(session_factory):
    chain = started_chain()
    scanner, _ = make_scanner(chain, session_factory, chunk_blocks=100)
    await scanner.run_once()
    chain.mine(500)
    chain.fail_on["get_logs"] = [RpcRateLimited("rate limited")]
    with pytest.raises(RpcRateLimited):
        await scanner.run_once()
    assert scanner.chunk_blocks == 50
    await scanner.run_once()
    assert await tip_height(session_factory) == 1495


async def test_other_upstream_errors_do_not_shrink_the_chunk(session_factory):
    chain = started_chain()
    scanner, _ = make_scanner(chain, session_factory, chunk_blocks=100)
    await scanner.run_once()
    chain.mine(500)
    chain.fail_on["get_headers"] = [UpstreamError("blip")]
    with pytest.raises(UpstreamError):
        await scanner.run_once()
    assert scanner.chunk_blocks == 100


async def test_a_hook_can_end_a_chunk_early_so_the_next_one_uses_fresh_filters(session_factory):
    chain = started_chain()
    hooks = CuttingHooks(1030)
    scanner, _ = make_scanner(chain, session_factory, hooks, chunk_blocks=1000)
    await scanner.run_once()  # initializes at 995
    chain.mine(50)  # blocks up to 1050 exist now, so logs can be attached to them
    chain.add_log(1010, WATCHED)
    chain.add_log(1040, WATCHED)
    await scanner.run_once()

    ranges = [(start, end) for start, end, _ in hooks.log_calls]
    assert ranges == [(996, 1045), (1031, 1045)]  # the hook cut the first chunk at 1030
    assert 1030 in await checkpoint_heights(session_factory)
    assert await tip_height(session_factory) == 1045


async def test_a_cut_at_the_chunk_start_makes_no_progress_but_the_scan_still_finishes(
    session_factory,
):
    chain = started_chain()
    hooks = CuttingHooks(995)
    scanner, _ = make_scanner(chain, session_factory, hooks, chunk_blocks=1000)
    await scanner.run_once()
    chain.mine(50)
    chain.add_log(1010, WATCHED)
    await scanner.run_once()
    assert [(start, end) for start, end, _ in hooks.log_calls] == [(996, 1045), (996, 1045)]
    assert await tip_height(session_factory) == 1045


async def test_a_cut_beyond_the_chunk_is_ignored(session_factory):
    chain = started_chain()
    hooks = CuttingHooks(5000)
    scanner, _ = make_scanner(chain, session_factory, hooks, chunk_blocks=1000)
    await scanner.run_once()
    chain.mine(50)
    chain.add_log(1010, WATCHED)
    await scanner.run_once()
    assert len(hooks.log_calls) == 1
    assert await tip_height(session_factory) == 1045


async def test_a_hook_that_never_lets_the_scan_advance_cannot_hang_it(session_factory):
    chain = started_chain()
    scanner, _ = make_scanner(chain, session_factory, AlwaysCutAtStart(), chunk_blocks=1000)
    await scanner.run_once()
    chain.mine(50)
    chain.add_log(1010, WATCHED)
    with pytest.raises(UpstreamError, match="no progress"):
        await scanner.run_once()
    assert await tip_height(session_factory) == 995