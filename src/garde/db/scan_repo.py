from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from garde.db.models import ProcessedBlock, ScanState


def _now() -> datetime:
    return datetime.now(UTC)


async def load_tip(session: AsyncSession, chain: str) -> ProcessedBlock | None:
    result = await session.execute(
        select(ProcessedBlock)
        .where(ProcessedBlock.chain == chain)
        .order_by(ProcessedBlock.height.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def load_checkpoints_desc(
    session: AsyncSession, chain: str, limit: int
) -> list[ProcessedBlock]:
    result = await session.execute(
        select(ProcessedBlock)
        .where(ProcessedBlock.chain == chain)
        .order_by(ProcessedBlock.height.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def add_checkpoint(session: AsyncSession, chain: str, height: int, block_hash: str) -> None:
    await session.merge(ProcessedBlock(chain=chain, height=height, block_hash=block_hash))


async def delete_above(session: AsyncSession, chain: str, height: int) -> None:
    await session.execute(
        delete(ProcessedBlock).where(ProcessedBlock.chain == chain, ProcessedBlock.height > height)
    )


async def prune_below(session: AsyncSession, chain: str, height: int) -> None:
    await session.execute(
        delete(ProcessedBlock).where(ProcessedBlock.chain == chain, ProcessedBlock.height < height)
    )


async def get_state(session: AsyncSession, chain: str) -> ScanState:
    state = await session.get(ScanState, chain)
    if state is None:
        state = ScanState(chain=chain, reorgs_total=0, deep_reorgs_total=0, gaps_total=0)
        session.add(state)
        await session.flush()
    return state


async def mark_success(session: AsyncSession, chain: str, head: int) -> None:
    state = await get_state(session, chain)
    state.last_head = head
    state.last_success_at = _now()
    state.last_error = None


async def record_reorg(session: AsyncSession, chain: str, *, rewind: int, deep: bool) -> None:
    state = await get_state(session, chain)
    state.reorgs_total += 1
    if deep:
        state.deep_reorgs_total += 1
    state.last_reorg_rewind_blocks = rewind
    state.last_reorg_at = _now()


async def record_gap(session: AsyncSession, chain: str, first: int, last: int) -> None:
    state = await get_state(session, chain)
    state.gaps_total += 1
    state.last_gap_first = first
    state.last_gap_last = last


async def record_error(session: AsyncSession, chain: str, message: str) -> None:
    state = await get_state(session, chain)
    state.last_error = message[:200]