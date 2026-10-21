from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from telegram.error import BadRequest, ChatMigrated, Forbidden, RetryAfter, TelegramError, TimedOut

from garde.db import event_repo, scan_repo
from garde.db.models import AlertEvent, Chat, Delivery, Token, Watch
from garde.delivery.sender import PassReport, seconds_of
from tests.delivery_helpers import (
    all_rows,
    chat_row,
    create_chat,
    create_event,
    create_token,
    create_watch,
    queue,
    set_scan_state,
)
from tests.fake_telegram import FakeMessenger, make_sender


class Stop(Exception):
    """Raised by a fake sleep to end run_forever in tests."""


async def one_pending(factory, telegram_id=111, **event_kwargs):
    chat = await create_chat(factory, telegram_id)
    token = await create_token(factory)
    await create_watch(factory, chat, token)
    event = await create_event(factory, token, **event_kwargs)
    await queue(factory, event, chat)
    return chat, token, event


async def two_chats_one_event(factory):
    token = await create_token(factory)
    chats = [await create_chat(factory, 111), await create_chat(factory, 222)]
    for chat in chats:
        await create_watch(factory, chat, token)
    event = await create_event(factory, token)
    for chat in chats:
        await queue(factory, event, chat)
    return chats, token, event


# --- the normal path ---------------------------------------------------------------------------


async def test_a_pending_alert_is_sent_once_and_marked_sent(session_factory):
    await one_pending(session_factory, explanation="Pool paused")
    messenger = FakeMessenger()
    sender = make_sender(session_factory, messenger)

    report = await sender.run_once()
    assert report.sent == 1
    ((chat_id, text),) = messenger.sent
    assert chat_id == 111
    assert "Pool paused" in text and "TKN" in text and "not financial advice" in text
    (delivery,) = await all_rows(session_factory, Delivery)
    assert delivery.sent_at is not None

    assert (await sender.run_once()).sent == 0
    assert len(messenger.sent) == 1


async def test_each_chat_gets_its_own_copy(session_factory):
    await two_chats_one_event(session_factory)
    messenger = FakeMessenger()
    await make_sender(session_factory, messenger).run_once()
    assert sorted(chat_id for chat_id, _ in messenger.sent) == [111, 222]


async def test_an_unsent_alert_for_a_retracted_event_is_never_sent(session_factory):
    await one_pending(session_factory, retracted_at=datetime.now(UTC))
    messenger = FakeMessenger()
    assert (await make_sender(session_factory, messenger).run_once()).sent == 0
    assert messenger.sent == []


async def test_a_notice_that_stopped_being_pending_is_not_sent(session_factory):
    await one_pending(session_factory)
    (delivery,) = await all_rows(session_factory, Delivery)
    async with session_factory() as session, session.begin():
        await session.execute(delete(Delivery))  # as if the chat unwatched meanwhile
    messenger = FakeMessenger()
    await make_sender(session_factory, messenger)._deliver(delivery.id, PassReport())
    assert messenger.sent == []


# --- retraction notices -------------------------------------------------------------------------


async def retracted_and_already_sent(factory, *, block=100, retracted_at=None):
    chat = await create_chat(factory, 111)
    token = await create_token(factory)
    await create_watch(factory, chat, token)
    event = await create_event(
        factory, token, block=block, retracted_at=retracted_at or datetime.now(UTC)
    )
    await queue(factory, event, chat, "alert", sent_at=datetime.now(UTC))


async def set_tip(factory, height: int) -> None:
    async with factory() as session, session.begin():
        await scan_repo.add_checkpoint(session, "robinhood", height, "0x" + "11" * 32)


async def test_a_retraction_is_sent_once_the_scan_has_passed_the_old_block(session_factory):
    await retracted_and_already_sent(session_factory)
    await set_tip(session_factory, 200)
    messenger = FakeMessenger()
    report = await make_sender(session_factory, messenger).run_once()
    assert report.sent == 1
    ((_, text),) = messenger.sent
    assert "retracted" in text.lower()


async def test_no_retraction_is_sent_while_the_scan_has_not_passed_the_block(session_factory):
    await retracted_and_already_sent(session_factory)
    await set_tip(session_factory, 50)
    messenger = FakeMessenger()
    await make_sender(session_factory, messenger).run_once()
    assert messenger.sent == []


async def test_retractions_older_than_the_window_are_left_alone(session_factory):
    await retracted_and_already_sent(
        session_factory, retracted_at=datetime.now(UTC) - timedelta(days=3)
    )
    await set_tip(session_factory, 200)
    messenger = FakeMessenger()
    await make_sender(session_factory, messenger).run_once()
    assert messenger.sent == []
    assert len(await all_rows(session_factory, Delivery)) == 1


async def test_a_reconfirmation_is_sent(session_factory):
    chat = await create_chat(session_factory, 111)
    token = await create_token(session_factory)
    await create_watch(session_factory, chat, token)
    event = await create_event(session_factory, token)
    await queue(session_factory, event, chat, "reconfirmed")
    messenger = FakeMessenger()
    await make_sender(session_factory, messenger).run_once()
    assert "back on chain" in messenger.sent[0][1]


# --- telegram errors -----------------------------------------------------------------------------


def test_retry_after_accepts_numbers_and_timedeltas():
    assert seconds_of(5) == 5.0
    assert seconds_of(timedelta(seconds=7)) == 7.0


async def test_a_flood_limit_stops_the_pass_and_keeps_everything_pending(session_factory):
    await two_chats_one_event(session_factory)
    messenger = FakeMessenger()
    messenger.script[111] = [RetryAfter(5)]
    sender = make_sender(session_factory, messenger)

    report = await sender.run_once()
    assert report.flood_wait == 5.0
    assert messenger.attempts == [111]  # the second chat was not even tried
    assert messenger.sent == []

    report = await sender.run_once()  # the limit has passed
    assert report.sent == 2


async def test_a_chat_that_blocked_the_bot_is_dropped_and_others_still_get_the_alert(
    session_factory,
):
    (blocked, other), _, _ = await two_chats_one_event(session_factory)
    messenger = FakeMessenger()
    messenger.script[111] = [Forbidden("bot was blocked by the user")]
    report = await make_sender(session_factory, messenger).run_once()

    assert (report.dropped, report.sent) == (1, 1)
    assert [chat_id for chat_id, _ in messenger.sent] == [222]
    assert [c.id for c in await all_rows(session_factory, Chat)] == [other]
    assert len(await all_rows(session_factory, Watch)) == 1
    assert len(await all_rows(session_factory, Token)) == 1
    (delivery,) = await all_rows(session_factory, Delivery)
    assert delivery.chat_id == other and delivery.sent_at is not None


async def test_dropping_the_last_watcher_cleans_the_token_and_its_events_up(session_factory):
    await one_pending(session_factory)
    messenger = FakeMessenger()
    messenger.script[111] = [Forbidden("kicked")]
    await make_sender(session_factory, messenger).run_once()
    for model in (Chat, Watch, Token, AlertEvent, Delivery):
        assert await all_rows(session_factory, model) == []


async def test_a_migrated_group_is_followed_to_its_new_id(session_factory):
    chat, _, _ = await one_pending(session_factory)
    messenger = FakeMessenger()
    messenger.script[111] = [ChatMigrated(-1001)]
    report = await make_sender(session_factory, messenger).run_once()
    assert report.sent == 1
    assert messenger.sent[0][0] == -1001
    assert (await chat_row(session_factory, chat)).telegram_chat_id == -1001


async def test_a_migration_to_an_id_that_already_exists_drops_the_old_chat(session_factory):
    await create_chat(session_factory, -1001)  # the supergroup already has its own row
    await one_pending(session_factory)
    messenger = FakeMessenger()
    messenger.script[111] = [ChatMigrated(-1001)]
    report = await make_sender(session_factory, messenger).run_once()
    assert report.sent == 0 and report.dropped == 1
    assert [c.telegram_chat_id for c in await all_rows(session_factory, Chat)] == [-1001]


async def test_a_rejected_message_is_forgotten_and_does_not_block_the_queue(session_factory):
    await two_chats_one_event(session_factory)
    messenger = FakeMessenger()
    messenger.script[111] = [BadRequest("message is too long")]
    report = await make_sender(session_factory, messenger).run_once()
    assert (report.dropped, report.sent) == (1, 1)
    assert [chat_id for chat_id, _ in messenger.sent] == [222]
    assert len(await all_rows(session_factory, Chat)) == 2  # BadRequest never drops the chat


async def test_a_network_problem_stops_the_pass_and_loses_nothing(session_factory):
    await two_chats_one_event(session_factory)
    messenger = FakeMessenger()
    messenger.script[111] = [TimedOut()]
    sender = make_sender(session_factory, messenger)

    report = await sender.run_once()
    assert report.network_problem is True
    assert messenger.sent == []
    assert all(d.sent_at is None for d in await all_rows(session_factory, Delivery))

    assert (await sender.run_once()).sent == 2


async def test_an_unknown_error_gives_up_on_that_notice_after_five_attempts(session_factory):
    await one_pending(session_factory)
    messenger = FakeMessenger()
    messenger.script[111] = [TelegramError("weird")] * 5
    sender = make_sender(session_factory, messenger)
    for _ in range(4):
        await sender.run_once()
    assert len(await all_rows(session_factory, Delivery)) == 1
    report = await sender.run_once()
    assert report.dropped == 1
    assert await all_rows(session_factory, Delivery) == []


# --- crashes ------------------------------------------------------------------------------------


async def test_a_crash_between_sending_and_saving_sent_at_resends_never_loses(
    session_factory, monkeypatch
):
    await one_pending(session_factory)
    messenger = FakeMessenger()
    sender = make_sender(session_factory, messenger)
    real = event_repo.mark_sent
    calls = {"count": 0}

    async def crashes_once(session, delivery_id):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("process died")
        await real(session, delivery_id)

    monkeypatch.setattr(event_repo, "mark_sent", crashes_once)
    with pytest.raises(RuntimeError):
        await sender.run_once()
    assert len(messenger.sent) == 1

    await sender.run_once()
    assert len(messenger.sent) == 2  # at-least-once: a duplicate, never a loss
    assert (await all_rows(session_factory, Delivery))[0].sent_at is not None


async def test_a_restarted_sender_delivers_what_was_committed_but_never_sent(session_factory):
    await one_pending(session_factory)  # committed by the scanner, then the process died
    messenger = FakeMessenger()
    report = await make_sender(session_factory, messenger).run_once()  # a brand new sender
    assert report.sent == 1


# --- gap notices ----------------------------------------------------------------------------------


async def watcher(factory, telegram_id, *, gaps_notified=0):
    chat = await create_chat(factory, telegram_id, gaps_notified=gaps_notified)
    token = await create_token(factory, address="0x" + f"{telegram_id:040x}")
    await create_watch(factory, chat, token)
    return chat


async def test_a_gap_notice_goes_once_to_chats_that_were_watching(session_factory):
    chat = await watcher(session_factory, 111)
    await set_scan_state(session_factory, gaps_total=2, last_gap_first=100, last_gap_last=500)
    messenger = FakeMessenger()
    sender = make_sender(session_factory, messenger)

    report = await sender.run_once()
    assert report.gap_notices == 1
    ((chat_id, text),) = messenger.sent
    assert chat_id == 111
    assert "2 stretches" in text and "blocks 100 to 500" in text
    assert (await chat_row(session_factory, chat)).gaps_notified == 2

    await sender.run_once()
    assert len(messenger.sent) == 1


async def test_chats_without_watches_or_created_after_the_gap_are_not_told(session_factory):
    await create_chat(session_factory, 111)  # no watches
    await watcher(session_factory, 222, gaps_notified=2)  # started after the gaps
    await set_scan_state(session_factory, gaps_total=2, last_gap_first=1, last_gap_last=2)
    messenger = FakeMessenger()
    await make_sender(session_factory, messenger).run_once()
    assert messenger.sent == []


async def test_a_gap_notice_that_cannot_be_delivered_is_retried_later(session_factory):
    chat = await watcher(session_factory, 111)
    await set_scan_state(session_factory, gaps_total=1, last_gap_first=1, last_gap_last=2)
    messenger = FakeMessenger()
    messenger.script[111] = [TimedOut()]
    sender = make_sender(session_factory, messenger)
    await sender.run_once()
    assert (await chat_row(session_factory, chat)).gaps_notified == 0
    await sender.run_once()
    assert (await chat_row(session_factory, chat)).gaps_notified == 1


# --- the loop -------------------------------------------------------------------------------------


async def delays_from(sender, run_once, stop_after: int) -> list[float]:
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)
        if len(delays) >= stop_after:
            raise Stop

    sender._sleep = fake_sleep
    sender.run_once = run_once
    with pytest.raises(Stop):
        await sender.run_forever()
    return delays


async def test_crashes_back_off_exponentially(session_factory):
    async def crashes():
        raise RuntimeError("x")

    sender = make_sender(session_factory, FakeMessenger())
    assert await delays_from(sender, crashes, 4) == [1.0, 2.0, 4.0, 8.0]


async def test_a_flood_wait_is_honoured_and_network_trouble_backs_off(session_factory):
    async def flooded():
        return PassReport(flood_wait=30.0)

    async def offline():
        return PassReport(network_problem=True)

    assert await delays_from(make_sender(session_factory, FakeMessenger()), flooded, 1) == [30.0]
    assert await delays_from(make_sender(session_factory, FakeMessenger()), offline, 2) == [
        1.0,
        2.0,
    ]