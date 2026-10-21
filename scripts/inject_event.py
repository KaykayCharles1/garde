"""Queue a clearly labelled TEST alert for every chat watching a token.

Run from the repo root:
    python scripts/inject_event.py robinhood 0xTOKEN [rule]

It writes to your real database so you can watch the sender deliver a real Telegram message.
The event sits at block 1, so a reorg never retracts it. Nothing happened on chain.
"""

import asyncio
import secrets
import sys

from sqlalchemy import select

from garde.chains.addresses import normalize_evm_address
from garde.config import Settings
from garde.db import event_repo
from garde.db.models import AlertEvent, Token, Watch
from garde.db.session import create_engine, create_session_factory
from garde.domain import Rule


async def main(chain: str, raw_address: str, rule: Rule) -> None:
    settings = Settings()
    address = normalize_evm_address(raw_address)
    engine = create_engine(settings.database_url)
    factory = create_session_factory(engine)
    async with factory() as session, session.begin():
        token = (
            await session.execute(
                select(Token).where(Token.chain == chain, Token.address == address)
            )
        ).scalar_one_or_none()
        if token is None:
            print("No chat is watching that token. Use /watch first.")
            await engine.dispose()
            return
        chat_ids = list(
            (await session.execute(select(Watch.chat_id).where(Watch.token_id == token.id)))
            .scalars()
            .all()
        )
        event = AlertEvent(
            chain=chain,
            tx_id="0x" + secrets.token_hex(32),
            log_index=0,
            rule=rule.value,
            token_id=token.id,
            severity="warning",
            explanation=(
                "TEST EVENT: this alert was injected by scripts/inject_event.py to test "
                "delivery. Nothing happened on chain."
            ),
            explorer_url=f"{settings.robinhood_explorer_url.rstrip('/')}/address/{address}",
            block_number=1,
            block_hash="0x" + "00" * 32,
        )
        session.add(event)
        await session.flush()
        await event_repo.queue_deliveries(session, event.id, chat_ids, "alert")
    print(f"Queued a test alert for {len(chat_ids)} chat(s).")
    await engine.dispose()


if __name__ == "__main__":
    if len(sys.argv) < 3:
        sys.exit("Usage: python scripts/inject_event.py <chain> <address> [rule]")
    asyncio.run(main(sys.argv[1], sys.argv[2], Rule(sys.argv[3] if len(sys.argv) > 3 else "paused")))