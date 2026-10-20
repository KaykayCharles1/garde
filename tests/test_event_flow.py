from datetime import UTC, datetime

from sqlalchemy import select, update

from garde.chains.evm_scanner import EvmScanner, ScannerConfig
from garde.chains.holders import ZERO_ADDRESS
from garde.db import event_repo, scan_repo
from garde.db.models import AlertEvent, Chat, Delivery, Token, Watch, WatchedWallet
from garde.domain import Chain
from garde.events.decoders import EVENT_TOPICS, OWNERSHIP_TRANSFERRED, TRANSFER, Thresholds
from garde.events.hooks import EventHooks, TokenView, WatchView, build_filters
from tests.sim_chain import SimChain, make_hash

TOKEN = "0x" + "aa" * 20
WALLET = "0x" + "bb" * 20
OWNER_A = "0x" + "cc" * 20
OWNER_B = "0x" + "dd" * 20
RECEIVER = "0x" + "ee" * 20
SAME_TX = make_hash("same-tx")
CONFIG = ScannerConfig(
    confirmations=5,
    chunk_blocks=100,
    max_backfill_blocks=10_000,
    hash_window_blocks=5_000,
    poll_interval=1.0,
    backoff_base=1.0,
    backoff_max=8.0,
)


def pad(address: str) -> str:
    return "0x" + "00" * 12 + address[2:]


def word(value: int) -> str:
    return f"{value:064x}"


def started_chain(length: int = 1000) -> SimChain:
    chain = SimChain()
    chain.mine(length)
    return chain


def add_mint(chain, height, amount, *, tx_hash=None, log_index=0):
    return chain.add_log(
        height,
        TOKEN,
        log_index=log_index,
        tx_hash=tx_hash,
        topics=[TRANSFER, pad(ZERO_ADDRESS), pad(RECEIVER)],
        data="0x" + word(amount),
    )


def add_transfer_out(chain, height, sender, amount, *, log_index=1):
    return chain.add_log(
        height,
        TOKEN,
        log_index=log_index,
        topics=[TRANSFER, pad(sender), pad(RECEIVER)],
        data="0x" + word(amount),
    )


def add_owner_change(chain, height, previous, new, *, log_index=2):
    return chain.add_log(
        height,
        TOKEN,
        log_index=log_index,
        topics=[OWNERSHIP_TRANSFERRED, pad(previous), pad(new)],
    )


def make_scanner(chain, factory):
    hooks = EventHooks(
        factory,
        chain=Chain.ROBINHOOD,
        explorer_url="https://explorer.invalid",
        thresholds=Thresholds(1.0, 5.0),
    )
    return EvmScanner(chain, factory, CONFIG, hooks), hooks


async def seed(factory, *, chats=1, start_block=0, supply="10000", owner=None, wallets=None,
               disabled=None):
    """One token watched by `chats` chats. wallets/disabled map chat index -> wallets/rules."""
    wallets = wallets or {}
    disabled = disabled or {}
    async with factory() as session, session.begin():
        token = Token(
            chain="robinhood",
            address=TOKEN,
            symbol="TKN",
            decimals=0,
            supply_snapshot=supply,
            owner_address=owner,
        )
        session.add(token)
        await session.flush()
        chat_ids = []
        for index in range(chats):
            chat = Chat(telegram_chat_id=1000 + index, kind="private")
            session.add(chat)
            await session.flush()
            session.add(
                Watch(
                    chat_id=chat.id,
                    token_id=token.id,
                    start_block=start_block,
                    disabled_rules=list(disabled.get(index, [])),
                    wallets=[
                        WatchedWallet(address=address, label=label)
                        for address, label in wallets.get(index, {}).items()
                    ],
                )
            )
            chat_ids.append(chat.id)
    return token.id, chat_ids


async def all_events(factory):
    async with factory() as session:
        return list((await session.execute(select(AlertEvent).order_by(AlertEvent.id))).scalars())


async def all_deliveries(factory):
    async with factory() as session:
        return list((await session.execute(select(Delivery).order_by(Delivery.id))).scalars())


async def mark_sent(factory, kind):
    async with factory() as session, session.begin():
        await session.execute(
            update(Delivery).where(Delivery.kind == kind).values(sent_at=datetime.now(UTC))
        )


async def pending(factory):
    async with factory() as session:
        return [(d.kind, d.chat_id) for d, _ in await event_repo.pending_deliveries(session)]


async def finalize(factory):
    async with factory() as session, session.begin():
        tip = await scan_repo.load_tip(session, "robinhood")
        await event_repo.finalize_retractions(session, "robinhood", tip.height)


async def owner_of(factory, token_id):
    async with factory() as session:
        return (await session.get(Token, token_id)).owner_address


def view(address, wallets=None, owner=None) -> TokenView:
    watch = WatchView(chat_id=1, start_block=0, disabled_rules=set(), wallets=wallets or {})
    return TokenView(
        token_id=1, address=address, symbol=None, decimals=None, supply=None, owner=owner,
        watches=[watch],
    )


# --- filters ----------------------------------------------------------------------------


def test_no_watched_tokens_means_no_filters():
    assert build_filters([], limit=500) == []


def test_filters_cover_events_mints_and_wallet_transfers():
    filters = build_filters([view(TOKEN, {WALLET: "treasury"}, owner=OWNER_A)], limit=500)
    assert len(filters) == 2
    assert filters[0] == {"address": [TOKEN], "topics": [list(EVENT_TOPICS)]}
    first, second = filters[1]["topics"]
    assert first == [TRANSFER]
    assert set(second) == {pad(ZERO_ADDRESS), pad(WALLET), pad(OWNER_A)}


def test_large_sets_are_split_into_chunks():
    addresses = ["0x" + f"{i:040x}" for i in range(1, 6)]
    wallets = {"0x" + f"{i:040x}": "w" for i in range(100, 105)}
    filters = build_filters([view(a, wallets) for a in addresses], limit=2)
    # 5 addresses -> 3 chunks; 5 wallets + the zero address -> 3 chunks of topics.
    assert len(filters) == 3 * (1 + 3)
    for log_filter in filters:
        assert len(log_filter["address"]) <= 2
        if len(log_filter["topics"]) == 2:
            assert len(log_filter["topics"][1]) <= 2


# --- from logs to events and deliveries -----------------------------------------------------


async def test_a_mint_after_the_watch_started_becomes_an_event_and_a_delivery(session_factory):
    _, chat_ids = await seed(session_factory)
    chain = started_chain()
    scanner, _ = make_scanner(chain, session_factory)
    await scanner.run_once()  # initializes at 995
    chain.mine(50)
    add_mint(chain, 1010, 100)
    await scanner.run_once()

    (event,) = await all_events(session_factory)
    assert event.rule == "supply_minted"
    assert (event.block_number, event.block_hash) == (1010, chain.hash_at(1010))
    assert event.retracted_at is None
    deliveries = await all_deliveries(session_factory)
    assert [(d.kind, d.chat_id, d.sent_at) for d in deliveries] == [("alert", chat_ids[0], None)]


async def test_events_at_or_before_the_start_block_are_not_stored(session_factory):
    await seed(session_factory, start_block=1010)
    chain = started_chain()
    scanner, _ = make_scanner(chain, session_factory)
    await scanner.run_once()
    chain.mine(50)
    add_mint(chain, 1010, 100)
    add_mint(chain, 1011, 100)
    await scanner.run_once()
    assert [e.block_number for e in await all_events(session_factory)] == [1011]


async def test_two_chats_share_one_event_and_each_get_a_delivery(session_factory):
    _, chat_ids = await seed(session_factory, chats=2)
    chain = started_chain()
    scanner, _ = make_scanner(chain, session_factory)
    await scanner.run_once()
    chain.mine(50)
    add_mint(chain, 1010, 100)
    await scanner.run_once()
    assert len(await all_events(session_factory)) == 1
    deliveries = await all_deliveries(session_factory)
    assert sorted(d.chat_id for d in deliveries) == sorted(chat_ids)


async def test_a_disabled_rule_is_skipped_for_that_chat_only(session_factory):
    _, chat_ids = await seed(session_factory, chats=2, disabled={0: ["supply_minted"]})
    chain = started_chain()
    scanner, _ = make_scanner(chain, session_factory)
    await scanner.run_once()
    chain.mine(50)
    add_mint(chain, 1010, 100)
    await scanner.run_once()
    deliveries = await all_deliveries(session_factory)
    assert [d.chat_id for d in deliveries] == [chat_ids[1]]


async def test_declared_wallets_alert_only_the_chat_that_declared_them(session_factory):
    _, chat_ids = await seed(session_factory, chats=2, wallets={0: {WALLET: "treasury"}})
    chain = started_chain()
    scanner, _ = make_scanner(chain, session_factory)
    await scanner.run_once()
    chain.mine(50)
    add_transfer_out(chain, 1010, WALLET, 1000)  # 10% of the 10,000 supply
    await scanner.run_once()

    (event,) = await all_events(session_factory)
    assert event.rule == "large_transfer_out"
    assert "treasury" in event.explanation
    assert [d.chat_id for d in await all_deliveries(session_factory)] == [chat_ids[0]]


async def test_the_owner_is_watched_for_every_chat(session_factory):
    _, chat_ids = await seed(session_factory, chats=2, owner=OWNER_A)
    chain = started_chain()
    scanner, _ = make_scanner(chain, session_factory)
    await scanner.run_once()
    chain.mine(50)
    add_transfer_out(chain, 1010, OWNER_A, 1000)
    await scanner.run_once()
    deliveries = await all_deliveries(session_factory)
    assert sorted(d.chat_id for d in deliveries) == sorted(chat_ids)


async def test_a_new_owner_is_watched_without_missing_the_same_chunk(session_factory):
    token_id, _ = await seed(session_factory, owner=OWNER_A)
    chain = started_chain()
    scanner, _ = make_scanner(chain, session_factory)
    await scanner.run_once()
    chain.mine(50)
    add_owner_change(chain, 1010, OWNER_A, OWNER_B)
    add_transfer_out(chain, 1020, OWNER_B, 1000)  # only fetched once OWNER_B is the owner
    await scanner.run_once()

    events = await all_events(session_factory)
    assert [e.rule for e in events] == ["ownership_transferred", "large_transfer_out"]
    assert await owner_of(session_factory, token_id) == OWNER_B
    assert len(await all_deliveries(session_factory)) == 2


async def test_renouncing_clears_the_owner(session_factory):
    token_id, _ = await seed(session_factory, owner=OWNER_A)
    chain = started_chain()
    scanner, _ = make_scanner(chain, session_factory)
    await scanner.run_once()
    chain.mine(50)
    add_owner_change(chain, 1010, OWNER_A, ZERO_ADDRESS)
    await scanner.run_once()
    assert [e.rule for e in await all_events(session_factory)] == ["ownership_renounced"]
    assert await owner_of(session_factory, token_id) is None


async def test_replaying_the_same_logs_creates_nothing_new(session_factory):
    await seed(session_factory)
    chain = started_chain()
    scanner, hooks = make_scanner(chain, session_factory)
    await scanner.run_once()
    chain.mine(50)
    log = add_mint(chain, 1010, 100)
    await scanner.run_once()
    before = (len(await all_events(session_factory)), len(await all_deliveries(session_factory)))

    async with session_factory() as session, session.begin():
        await hooks.on_logs(session, [log], 1010, 1010)

    after = (len(await all_events(session_factory)), len(await all_deliveries(session_factory)))
    assert before == after == (1, 1)


# --- reorgs: retraction notices -------------------------------------------------------------


async def test_a_reorged_away_alert_is_retracted_and_chats_that_got_it_are_told(session_factory):
    _, chat_ids = await seed(session_factory)
    chain = started_chain()
    scanner, _ = make_scanner(chain, session_factory)
    await scanner.run_once()
    chain.mine(50)
    add_mint(chain, 1010, 100)
    await scanner.run_once()
    await mark_sent(session_factory, "alert")

    chain.reorg(fork_height=1005, new_blocks=60)  # block 1010 is replaced
    await scanner.run_once()
    (event,) = await all_events(session_factory)
    assert event.retracted_at is not None
    assert await pending(session_factory) == []  # nothing is announced until the rescan is done

    await finalize(session_factory)
    deliveries = await all_deliveries(session_factory)
    assert sorted(d.kind for d in deliveries) == ["alert", "retraction"]
    assert await pending(session_factory) == [("retraction", chat_ids[0])]


async def test_an_unsent_alert_for_a_reorged_event_is_never_sent(session_factory):
    await seed(session_factory)
    chain = started_chain()
    scanner, _ = make_scanner(chain, session_factory)
    await scanner.run_once()
    chain.mine(50)
    add_mint(chain, 1010, 100)
    await scanner.run_once()
    assert len(await pending(session_factory)) == 1  # queued, not sent yet

    chain.reorg(fork_height=1005, new_blocks=60)
    await scanner.run_once()
    await finalize(session_factory)

    assert [d.kind for d in await all_deliveries(session_factory)] == ["alert"]
    assert await pending(session_factory) == []  # skipped, and no retraction for a non-receiver


async def test_an_event_that_reappears_in_the_new_fork_is_not_retracted(session_factory):
    await seed(session_factory)
    chain = started_chain()
    scanner, _ = make_scanner(chain, session_factory)
    await scanner.run_once()
    chain.mine(50)
    add_mint(chain, 1010, 100, tx_hash=SAME_TX)
    await scanner.run_once()
    await mark_sent(session_factory, "alert")

    chain.reorg(fork_height=1005, new_blocks=60)
    add_mint(chain, 1012, 100, tx_hash=SAME_TX)  # same transaction, new block
    await scanner.run_once()
    await finalize(session_factory)

    (event,) = await all_events(session_factory)
    assert event.retracted_at is None
    assert (event.block_number, event.block_hash) == (1012, chain.hash_at(1012))
    assert [d.kind for d in await all_deliveries(session_factory)] == ["alert"]
    assert await pending(session_factory) == []


async def test_an_event_that_returns_after_a_retraction_notice_is_reconfirmed(session_factory):
    _, chat_ids = await seed(session_factory)
    chain = started_chain()
    scanner, _ = make_scanner(chain, session_factory)
    await scanner.run_once()
    chain.mine(50)
    add_mint(chain, 1010, 100, tx_hash=SAME_TX)
    await scanner.run_once()
    await mark_sent(session_factory, "alert")

    chain.reorg(fork_height=1005, new_blocks=60)
    await scanner.run_once()
    await finalize(session_factory)
    await mark_sent(session_factory, "retraction")

    chain.mine(40)
    add_mint(chain, 1080, 100, tx_hash=SAME_TX)  # the transaction is mined again, later
    await scanner.run_once()

    (event,) = await all_events(session_factory)
    assert event.retracted_at is None
    deliveries = await all_deliveries(session_factory)
    assert sorted(d.kind for d in deliveries) == ["alert", "reconfirmed", "retraction"]
    assert await pending(session_factory) == [("reconfirmed", chat_ids[0])]