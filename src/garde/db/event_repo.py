"""Storing risk events and their per-chat deliveries. Everything here is idempotent."""

from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from garde.db.models import AlertEvent, Delivery
from garde.domain import RiskEvent

Outcome = Literal["new", "refreshed", "unchanged"]
_DEDUP_COLUMNS = ["chain", "tx_id", "log_index", "rule"]


def _now() -> datetime:
    return datetime.now(UTC)


async def _chat_ids(
    session: AsyncSession, event_id: int, kind: str, *, sent_only: bool
) -> list[int]:
    stmt = select(Delivery.chat_id).where(
        Delivery.alert_event_id == event_id, Delivery.kind == kind
    )
    if sent_only:
        stmt = stmt.where(Delivery.sent_at.is_not(None))
    return list((await session.execute(stmt)).scalars().all())


async def queue_deliveries(
    session: AsyncSession, event_id: int, chat_ids: list[int], kind: str
) -> None:
    """One row per chat. Queuing twice is harmless: the unique key ignores the repeat."""
    if not chat_ids:
        return
    stmt = (
        sqlite_insert(Delivery)
        .values([{"alert_event_id": event_id, "chat_id": c, "kind": kind} for c in chat_ids])
        .on_conflict_do_nothing(index_elements=["alert_event_id", "chat_id", "kind"])
    )
    await session.execute(stmt)


async def store_event(
    session: AsyncSession, event: RiskEvent, token_id: int, block_hash: str
) -> tuple[int, Outcome]:
    """Insert the event once. Returns (event id, what happened).

    "new": first time seen. "unchanged": already stored in the same block. "refreshed": it was
    retracted or moved to another block (it came back after a reorg). If a retraction notice
    had already been sent, the chats that got it are queued a "reconfirmed" notice.
    """
    stmt = (
        sqlite_insert(AlertEvent)
        .values(
            chain=event.chain.value,
            tx_id=event.tx_id,
            log_index=event.log_index,
            rule=event.rule.value,
            token_id=token_id,
            severity=event.severity.value,
            explanation=event.explanation,
            explorer_url=event.explorer_url,
            block_number=event.block_number,
            block_hash=block_hash,
        )
        .on_conflict_do_nothing(index_elements=_DEDUP_COLUMNS)
        .returning(AlertEvent.id)
    )
    new_id = (await session.execute(stmt)).scalar_one_or_none()
    if new_id is not None:
        return new_id, "new"

    row = (
        await session.execute(
            select(AlertEvent).where(
                AlertEvent.chain == event.chain.value,
                AlertEvent.tx_id == event.tx_id,
                AlertEvent.log_index == event.log_index,
                AlertEvent.rule == event.rule.value,
            )
        )
    ).scalar_one()
    was_retracted = row.retracted_at is not None
    if not was_retracted and row.block_hash == block_hash:
        return row.id, "unchanged"
    row.block_number = event.block_number
    row.block_hash = block_hash
    row.retracted_at = None
    if was_retracted:
        told = await _chat_ids(session, row.id, "retraction", sent_only=True)
        await queue_deliveries(session, row.id, told, "reconfirmed")
    return row.id, "refreshed"


async def mark_retracted_above(session: AsyncSession, chain: str, height: int) -> int:
    """Provisionally retract events above a reorg's fork. The rescan may bring them back."""
    result = await session.execute(
        update(AlertEvent)
        .where(
            AlertEvent.chain == chain,
            AlertEvent.block_number > height,
            AlertEvent.retracted_at.is_(None),
        )
        .values(retracted_at=_now())
    )
    return result.rowcount


async def finalize_retractions(
    session: AsyncSession, chain: str, tip: int, *, since: datetime | None = None
) -> None:
    """Once the scan has passed an event's old block, queue retraction notices for it.

    Only chats that actually received the alert are told. Safe to call repeatedly. `since`
    bounds the work to events retracted recently, so old ones are not re-checked forever.
    """
    stmt = select(AlertEvent.id).where(
        AlertEvent.chain == chain,
        AlertEvent.retracted_at.is_not(None),
        AlertEvent.block_number <= tip,
    )
    if since is not None:
        stmt = stmt.where(AlertEvent.retracted_at >= since)
    for event_id in (await session.execute(stmt)).scalars().all():
        received = await _chat_ids(session, event_id, "alert", sent_only=True)
        await queue_deliveries(session, event_id, received, "retraction")


async def pending_deliveries(
    session: AsyncSession, limit: int = 100, *, delivery_id: int | None = None
) -> list[tuple[Delivery, AlertEvent]]:
    """What the sender should send now, oldest first.

    An unsent alert for a retracted event is skipped, and a retraction notice is only sent
    while the event is still retracted. `delivery_id` re-checks one specific delivery.
    """
    live = and_(Delivery.kind.in_(("alert", "reconfirmed")), AlertEvent.retracted_at.is_(None))
    gone = and_(Delivery.kind == "retraction", AlertEvent.retracted_at.is_not(None))
    stmt = (
        select(Delivery, AlertEvent)
        .join(AlertEvent, Delivery.alert_event_id == AlertEvent.id)
        .where(Delivery.sent_at.is_(None), or_(live, gone))
        .order_by(Delivery.id)
        .limit(limit)
    )
    if delivery_id is not None:
        stmt = stmt.where(Delivery.id == delivery_id)
    return [(row[0], row[1]) for row in (await session.execute(stmt)).all()]


async def mark_sent(session: AsyncSession, delivery_id: int) -> None:
    await session.execute(
        update(Delivery).where(Delivery.id == delivery_id).values(sent_at=_now())
    )


async def delete_delivery(session: AsyncSession, delivery_id: int) -> None:
    await session.execute(delete(Delivery).where(Delivery.id == delivery_id))