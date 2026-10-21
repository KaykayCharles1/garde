from itertools import count

from sqlalchemy import select

from garde.db.models import AlertEvent, Chat, Delivery, ScanState, Token, Watch

TOKEN = "0xaa" + "aa" * 19
_tx_numbers = count(1)


async def create_chat(factory, telegram_id: int, *, gaps_notified: int = 0) -> int:
    async with factory() as session, session.begin():
        chat = Chat(telegram_chat_id=telegram_id, kind="private", gaps_notified=gaps_notified)
        session.add(chat)
        await session.flush()
        return chat.id


async def create_token(factory, address: str = TOKEN, symbol: str = "TKN", chain="robinhood"):
    async with factory() as session, session.begin():
        token = Token(chain=chain, address=address, symbol=symbol)
        session.add(token)
        await session.flush()
        return token.id


async def create_watch(factory, chat_id: int, token_id: int) -> None:
    async with factory() as session, session.begin():
        session.add(Watch(chat_id=chat_id, token_id=token_id, start_block=0, disabled_rules=[]))


async def create_event(
    factory,
    token_id: int,
    *,
    block: int = 100,
    rule: str = "paused",
    severity: str = "critical",
    explanation: str = "The token contract was paused.",
    retracted_at=None,
) -> int:
    async with factory() as session, session.begin():
        event = AlertEvent(
            chain="robinhood",
            tx_id="0x" + f"{next(_tx_numbers):064x}",
            log_index=0,
            rule=rule,
            token_id=token_id,
            severity=severity,
            explanation=explanation,
            explorer_url="https://explorer.invalid/tx/1",
            block_number=block,
            block_hash="0x" + "ab" * 32,
            retracted_at=retracted_at,
        )
        session.add(event)
        await session.flush()
        return event.id


async def queue(factory, event_id: int, chat_id: int, kind: str = "alert", sent_at=None) -> None:
    async with factory() as session, session.begin():
        session.add(Delivery(alert_event_id=event_id, chat_id=chat_id, kind=kind, sent_at=sent_at))


async def all_rows(factory, model) -> list:
    async with factory() as session:
        return list((await session.execute(select(model).order_by(model.id))).scalars())


async def set_scan_state(factory, **fields) -> None:
    async with factory() as session, session.begin():
        session.add(
            ScanState(
                chain="robinhood",
                reorgs_total=0,
                deep_reorgs_total=0,
                gaps_total=fields.pop("gaps_total", 0),
                **fields,
            )
        )


async def chat_row(factory, chat_id: int) -> Chat:
    async with factory() as session:
        return await session.get(Chat, chat_id)