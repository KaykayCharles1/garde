import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from garde.chains.errors import NotATokenError, UpstreamError
from garde.chains.fake import FAKE_DEPLOYER, FAKE_TOP_WALLETS, FakeAdapter
from garde.db.models import AlertEvent, Chat, Delivery, Token, Watch
from garde.domain import Chain
from garde.watch_service import AddResult, LimitReached, ServiceConfig, WatchError, WatchService

WALLET_X = "0x" + "cc" * 20
BASE = ServiceConfig(max_per_chat=5, max_global=100, confirmations=50, large_transfer_pct=1.0)


def token(n: int) -> str:
    return "0x" + f"{n:040x}"


def make_service(factory, *, adapter=None, adapters=None, **overrides):
    adapter = adapter or FakeAdapter()

    async def head() -> int:
        return 1000

    service = WatchService(
        factory,
        {Chain.ROBINHOOD: adapter} if adapters is None else adapters,
        {Chain.ROBINHOOD: head},
        replace(BASE, **overrides),
    )
    return service, adapter


async def add(service, n, *, chat=1, extras=()):
    return await service.add_watch(
        telegram_chat_id=chat,
        chat_kind="private",
        chain=Chain.ROBINHOOD,
        address=token(n),
        extra_wallets=extras,
    )


async def rows(factory, model):
    async with factory() as session:
        return list((await session.execute(select(model))).scalars())


async def row_id(factory, model, **criteria):
    async with factory() as session:
        return (await session.execute(select(model).filter_by(**criteria))).scalar_one().id


# --- adding -----------------------------------------------------------------------------------


async def test_adding_a_watch_stores_the_token_snapshot_and_all_wallets(session_factory):
    service, _ = make_service(session_factory)
    result = await add(service, 1, extras=[("treasury", WALLET_X)])

    labels = dict(result.wallets)
    assert labels[FAKE_DEPLOYER] == "deployer"
    assert all(labels[w] == "top holder" for w in FAKE_TOP_WALLETS)
    assert labels[WALLET_X] == "treasury"
    assert result.already_watching is False
    assert result.start_block == 950  # head 1000 minus 50 confirmations

    (stored_token,) = await rows(session_factory, Token)
    assert (stored_token.symbol, stored_token.decimals) == ("FAKE", 18)
    assert stored_token.supply_snapshot == "1000000"
    (stored_watch,) = await rows(session_factory, Watch)
    assert stored_watch.start_block == 950
    assert len(stored_watch.wallets) == 5
    (chat,) = await rows(session_factory, Chat)
    assert (chat.telegram_chat_id, chat.kind) == (1, "private")


async def test_the_owner_is_stored_on_the_token(session_factory):
    adapter = FakeAdapter()
    adapter.owner = "0x" + "22" * 20
    service, _ = make_service(session_factory, adapter=adapter)
    result = await add(service, 1)
    assert result.owner == "0x" + "22" * 20
    assert (await rows(session_factory, Token))[0].owner_address == "0x" + "22" * 20


async def test_a_label_the_user_chose_beats_the_automatic_label(session_factory):
    service, _ = make_service(session_factory)
    result = await add(service, 1, extras=[("my-deployer", FAKE_DEPLOYER)])
    assert dict(result.wallets)[FAKE_DEPLOYER] == "my-deployer"
    assert len(result.wallets) == 4  # deployer + three holders, no duplicate


async def test_bad_wallet_arguments_are_rejected_with_a_clear_message(session_factory):
    service, _ = make_service(session_factory, max_extra_wallets=2)
    with pytest.raises(WatchError, match="not a valid"):
        await add(service, 1, extras=[("x", "0x123")])
    with pytest.raises(WatchError, match="Burn"):
        await add(service, 1, extras=[("x", "0x" + "00" * 20)])
    with pytest.raises(WatchError, match="own address"):
        await add(service, 1, extras=[("x", token(1))])
    many = [(f"w{i}", "0x" + f"{i + 10:040x}") for i in range(3)]
    with pytest.raises(WatchError, match="At most 2"):
        await add(service, 1, extras=many)
    assert await rows(session_factory, Watch) == []


async def test_an_unsupported_chain_is_refused(session_factory):
    service, _ = make_service(session_factory, adapters={})
    with pytest.raises(WatchError, match="not supported"):
        await add(service, 1)


async def test_watching_again_reports_it_and_merges_new_wallets(session_factory):
    service, _ = make_service(session_factory)
    await add(service, 1)
    again = await add(service, 1, extras=[("treasury", WALLET_X)])
    assert again.already_watching is True
    assert again.new_wallets == 1
    assert len(await rows(session_factory, Watch)) == 1
    assert len((await rows(session_factory, Watch))[0].wallets) == 5

    repeat = await add(service, 1, extras=[("treasury", WALLET_X)])
    assert repeat.new_wallets == 0


async def test_a_watch_cannot_grow_past_the_wallet_limit(session_factory):
    service, _ = make_service(session_factory, max_wallets_per_watch=5)
    await add(service, 1)  # deployer + 3 holders = 4 wallets
    with pytest.raises(WatchError, match="at most 5"):
        await add(service, 1, extras=[("a", token(50)), ("b", token(51))])
    assert len((await rows(session_factory, Watch))[0].wallets) == 4


async def test_provider_problems_store_nothing(session_factory):
    adapter = FakeAdapter()
    service, _ = make_service(session_factory, adapter=adapter)
    adapter.snapshot_error = UpstreamError("down")
    with pytest.raises(UpstreamError):
        await add(service, 1)
    adapter.snapshot_error = NotATokenError("This address is a wallet, not a token.")
    with pytest.raises(NotATokenError):
        await add(service, 1)
    assert await rows(session_factory, Token) == []
    assert await rows(session_factory, Chat) == []


# --- limits -------------------------------------------------------------------------------------


async def test_the_per_chat_limit_applies_and_leaves_nothing_behind(session_factory):
    service, _ = make_service(session_factory, max_per_chat=1)
    await add(service, 1)
    with pytest.raises(LimitReached, match="limit"):
        await add(service, 2)
    assert len(await rows(session_factory, Token)) == 1  # the rejected token was rolled back
    await add(service, 2, chat=2)  # another chat is unaffected


async def test_the_global_limit_counts_distinct_tokens_so_sharing_is_free(session_factory):
    service, _ = make_service(session_factory, max_global=1)
    await add(service, 1, chat=1)
    await add(service, 1, chat=2)  # the same token: no new capacity needed
    with pytest.raises(LimitReached, match="capacity"):
        await add(service, 2, chat=3)
    assert len(await rows(session_factory, Token)) == 1
    assert len(await rows(session_factory, Watch)) == 2


async def test_simultaneous_watches_cannot_exceed_the_per_chat_limit(session_factory):
    service, _ = make_service(session_factory, max_per_chat=1)
    results = await asyncio.gather(
        add(service, 1, chat=7), add(service, 2, chat=7), return_exceptions=True
    )
    assert sum(isinstance(r, AddResult) for r in results) == 1
    assert sum(isinstance(r, LimitReached) for r in results) == 1
    assert len(await rows(session_factory, Watch)) == 1


async def test_simultaneous_watches_cannot_exceed_the_global_limit(session_factory):
    service, _ = make_service(session_factory, max_global=1)
    results = await asyncio.gather(
        add(service, 1, chat=1), add(service, 2, chat=2), return_exceptions=True
    )
    assert sum(isinstance(r, AddResult) for r in results) == 1
    assert sum(isinstance(r, LimitReached) for r in results) == 1
    assert len(await rows(session_factory, Token)) == 1


# --- removing and listing -------------------------------------------------------------------------


async def test_removing_a_watch_that_does_not_exist_is_a_quiet_false(session_factory):
    service, _ = make_service(session_factory)
    remove = service.remove_watch
    assert await remove(telegram_chat_id=1, chain=Chain.ROBINHOOD, address=token(1)) is False
    await add(service, 1, chat=1)
    assert await remove(telegram_chat_id=2, chain=Chain.ROBINHOOD, address=token(1)) is False
    assert await remove(telegram_chat_id=1, chain=Chain.ROBINHOOD, address=token(2)) is False


async def test_removing_drops_only_this_chats_unsent_notices_and_cleans_up_last(session_factory):
    service, _ = make_service(session_factory)
    await add(service, 1, chat=1)
    await add(service, 1, chat=2)
    token_id = await row_id(session_factory, Token, address=token(1))
    chat1 = await row_id(session_factory, Chat, telegram_chat_id=1)
    chat2 = await row_id(session_factory, Chat, telegram_chat_id=2)

    async with session_factory() as session, session.begin():
        event = AlertEvent(
            chain="robinhood", tx_id="0x1", log_index=0, rule="paused", token_id=token_id,
            severity="critical", explanation="x", explorer_url="u", block_number=1, block_hash="0xh",
        )
        session.add(event)
        await session.flush()
        session.add_all(
            [
                Delivery(alert_event_id=event.id, chat_id=chat1, kind="alert"),
                Delivery(
                    alert_event_id=event.id, chat_id=chat1, kind="retraction",
                    sent_at=datetime.now(UTC),
                ),
                Delivery(alert_event_id=event.id, chat_id=chat2, kind="alert"),
            ]
        )

    removed = await service.remove_watch(
        telegram_chat_id=1, chain=Chain.ROBINHOOD, address=token(1)
    )
    assert removed is True
    left = {(d.chat_id, d.kind) for d in await rows(session_factory, Delivery)}
    assert left == {(chat1, "retraction"), (chat2, "alert")}  # sent history stays
    assert len(await rows(session_factory, Token)) == 1  # chat 2 still watches it

    await service.remove_watch(telegram_chat_id=2, chain=Chain.ROBINHOOD, address=token(1))
    assert await rows(session_factory, Token) == []
    assert await rows(session_factory, AlertEvent) == []  # cascaded with the token
    assert await rows(session_factory, Delivery) == []
    assert await rows(session_factory, Watch) == []


async def test_listing_shows_a_chats_watches_in_order(session_factory):
    service, _ = make_service(session_factory)
    await add(service, 2, chat=1, extras=[("t", WALLET_X)])
    await add(service, 1, chat=1)
    await add(service, 3, chat=2)
    summaries = await service.list_watches(telegram_chat_id=1)
    assert [s.address for s in summaries] == [token(2), token(1)]
    assert [s.wallet_count for s in summaries] == [5, 4]
    assert await service.list_watches(telegram_chat_id=99) == []