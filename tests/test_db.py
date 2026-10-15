import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from garde.db.models import AlertEvent, Token, Watch
from garde.db.repo import record_alert_event
from garde.domain import Chain, Rule
from tests.test_domain import TOKEN, make_event


async def make_token(session) -> Token:
    token = Token(chain="robinhood", address=TOKEN)
    session.add(token)
    await session.flush()
    return token


async def count_events(session) -> int:
    return (await session.execute(select(func.count()).select_from(AlertEvent))).scalar_one()


async def test_database_itself_rejects_duplicate_event(session):
    token = await make_token(session)
    row = dict(chain="robinhood", tx_id="0xaa", log_index=1, rule="paused", token_id=token.id,
               severity="info", explanation="x", explorer_url="u")
    session.add_all([AlertEvent(**row), AlertEvent(**row)])
    with pytest.raises(IntegrityError):
        await session.commit()


async def test_record_alert_event_is_idempotent(session):
    token = await make_token(session)
    event = make_event()
    first = await record_alert_event(session, event, token.id)
    second = await record_alert_event(session, event, token.id)
    assert first is not None
    assert second is None
    assert await count_events(session) == 1
    stored = (await session.execute(select(AlertEvent))).scalar_one()
    assert stored.created_at is not None


async def test_different_rule_or_log_index_are_distinct_events(session):
    token = await make_token(session)
    await record_alert_event(session, make_event(), token.id)
    await record_alert_event(session, make_event(rule=Rule.PAUSED), token.id)
    await record_alert_event(session, make_event(log_index=4), token.id)
    assert await count_events(session) == 3


async def test_same_hash_in_different_case_still_dedups(session):
    token = await make_token(session)
    await record_alert_event(session, make_event(tx_id="0x" + "cd" * 32), token.id)
    again = await record_alert_event(session, make_event(tx_id="0x" + "CD" * 32), token.id)
    assert again is None


async def test_same_tx_on_different_chains_is_not_a_duplicate(session):
    token = await make_token(session)
    await record_alert_event(session, make_event(chain=Chain.ROBINHOOD, tx_id="same"), token.id)
    other = await record_alert_event(
        session, make_event(chain=Chain.SOLANA, tx_id="same"), token.id
    )    
    assert other is not None


async def test_foreign_keys_are_enforced(session):
    session.add(Watch(chat_id=999, token_id=999))
    with pytest.raises(IntegrityError):
        await session.commit()