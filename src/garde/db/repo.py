from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from garde.db.models import AlertEvent
from garde.domain import RiskEvent


async def record_alert_event(session: AsyncSession, event: RiskEvent, token_id: int) -> int | None:
    """Store an event once. Returns the new row id, or None if it was already recorded.

    The database decides, in a single atomic statement. Two scanners or a restart
    mid-scan cannot both insert the same event.
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
        )
        .on_conflict_do_nothing(index_elements=["chain", "tx_id", "log_index", "rule"])
        .returning(AlertEvent.id)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()