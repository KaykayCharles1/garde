from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError

from garde.bot.watch_handlers import list_command, unwatch, watch
from garde.chains.errors import NotATokenError, UpstreamError
from garde.chains.fake import FakeAdapter
from garde.domain import Chain
from garde.watch_service import ServiceConfig, WatchService

TOKEN = "0x" + "ab" * 20
WALLET = "0x" + "cc" * 20
CONFIG = ServiceConfig(max_per_chat=2, max_global=100, confirmations=50, large_transfer_pct=1.0)


def make_service(factory, adapter=None, config=CONFIG):
    adapter = adapter or FakeAdapter()

    async def head() -> int:
        return 1000

    return WatchService(factory, {Chain.ROBINHOOD: adapter}, {Chain.ROBINHOOD: head}, config)


def make_update(*, chat_type=ChatType.PRIVATE, chat_id=42, sender_chat=None, user_id=7):
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    update.message.sender_chat = sender_chat
    update.effective_chat.id = chat_id
    update.effective_chat.type = chat_type
    update.effective_user = SimpleNamespace(id=user_id) if user_id is not None else None
    return update


def make_context(args, service, member_status=ChatMemberStatus.MEMBER):
    context = MagicMock()
    context.args = args
    context.application.bot_data = {"watch_service": service}
    context.bot.get_chat_member = AsyncMock(return_value=SimpleNamespace(status=member_status))
    return context


async def reply(handler, args, service, update=None, **context_kwargs) -> str:
    update = update or make_update()
    await handler(update, make_context(args, service, **context_kwargs))
    update.message.reply_text.assert_awaited_once()
    return update.message.reply_text.await_args.args[0]


# --- /watch -------------------------------------------------------------------------------------


async def test_watch_in_a_private_chat_stores_and_explains(session_factory):
    service = make_service(session_factory)
    text = await reply(watch, ["robinhood", TOKEN], service)
    assert "Now watching FAKE on robinhood" in text
    assert "deployer" in text
    assert "not financial advice" in text
    assert len(await service.list_watches(telegram_chat_id=42)) == 1


async def test_a_labelled_wallet_is_stored_and_shown(session_factory):
    service = make_service(session_factory)
    text = await reply(watch, ["robinhood", TOKEN, f"treasury={WALLET}"], service)
    assert "treasury" in text
    (summary,) = await service.list_watches(telegram_chat_id=42)
    assert summary.wallet_count == 5


async def test_watching_twice_says_so(session_factory):
    service = make_service(session_factory)
    await reply(watch, ["robinhood", TOKEN], service)
    assert "Already watching" in await reply(watch, ["robinhood", TOKEN], service)


@pytest.mark.parametrize("status", [ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED])
async def test_an_ordinary_group_member_cannot_watch(session_factory, status):
    service = make_service(session_factory)
    update = make_update(chat_type=ChatType.GROUP)
    text = await reply(watch, ["robinhood", TOKEN], service, update, member_status=status)
    assert "administrators" in text
    assert await service.list_watches(telegram_chat_id=42) == []


@pytest.mark.parametrize("status", [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER])
async def test_group_admins_and_the_owner_can_watch(session_factory, status):
    service = make_service(session_factory)
    update = make_update(chat_type=ChatType.SUPERGROUP)
    text = await reply(watch, ["robinhood", TOKEN], service, update, member_status=status)
    assert "Now watching" in text


async def test_an_anonymous_group_admin_is_allowed_without_asking_telegram(session_factory):
    service = make_service(session_factory)
    update = make_update(chat_type=ChatType.SUPERGROUP, chat_id=-100, user_id=None)
    update.message.sender_chat = SimpleNamespace(id=-100)  # the group itself
    context = make_context(["robinhood", TOKEN], service)
    await watch(update, context)
    assert "Now watching" in update.message.reply_text.await_args.args[0]
    context.bot.get_chat_member.assert_not_awaited()


async def test_posting_as_another_channel_does_not_make_you_an_admin(session_factory):
    service = make_service(session_factory)
    update = make_update(chat_type=ChatType.SUPERGROUP, chat_id=-100, user_id=None)
    update.message.sender_chat = SimpleNamespace(id=-555)  # some other channel
    assert "administrators" in await reply(watch, ["robinhood", TOKEN], service, update)
    assert await service.list_watches(telegram_chat_id=-100) == []


async def test_a_failed_permission_lookup_is_friendly_and_changes_nothing(session_factory):
    service = make_service(session_factory)
    update = make_update(chat_type=ChatType.GROUP)
    context = make_context(["robinhood", TOKEN], service)
    context.bot.get_chat_member = AsyncMock(side_effect=TelegramError("boom"))
    await watch(update, context)
    text = update.message.reply_text.await_args.args[0]
    assert "Could not check" in text
    assert "boom" not in text
    assert await service.list_watches(telegram_chat_id=42) == []


async def test_watch_input_problems(session_factory):
    service = make_service(session_factory)
    assert "Usage" in await reply(watch, ["robinhood"], service)
    assert "Unknown chain" in await reply(watch, ["dogechain", TOKEN], service)
    assert "not a valid" in await reply(watch, ["robinhood", "0x123"], service)
    assert "not a valid" in await reply(watch, ["robinhood", TOKEN, "treasury=0x12"], service)
    assert "Burn" in await reply(watch, ["robinhood", TOKEN, "0x" + "00" * 20], service)


async def test_the_chat_limit_is_explained(session_factory):
    service = make_service(session_factory)
    await reply(watch, ["robinhood", "0x" + "01" * 20], service)
    await reply(watch, ["robinhood", "0x" + "02" * 20], service)
    assert "limit" in await reply(watch, ["robinhood", "0x" + "03" * 20], service)


async def test_not_a_token_message_is_passed_through(session_factory):
    adapter = FakeAdapter()
    adapter.snapshot_error = NotATokenError("This address is a wallet, not a token.")
    service = make_service(session_factory, adapter)
    assert "wallet, not a token" in await reply(watch, ["robinhood", TOKEN], service)


async def test_provider_failures_are_friendly_and_hide_details(session_factory):
    adapter = FakeAdapter()
    service = make_service(session_factory, adapter)
    adapter.snapshot_error = UpstreamError("secret provider detail")
    text = await reply(watch, ["robinhood", TOKEN], service)
    assert "unavailable" in text
    assert "secret" not in text

    adapter.snapshot_error = RuntimeError("internal boom")
    text = await reply(watch, ["robinhood", TOKEN], service)
    assert "went wrong" in text
    assert "boom" not in text


# --- /unwatch and /list ---------------------------------------------------------------------------


async def test_unwatch_and_list_flow(session_factory):
    service = make_service(session_factory)
    assert "not watching anything" in await reply(list_command, [], service)

    await reply(watch, ["robinhood", TOKEN], service)
    listing = await reply(list_command, [], service)
    assert "Watching 1 of 2 tokens" in listing
    assert "FAKE" in listing

    assert "Stopped watching" in await reply(unwatch, ["robinhood", TOKEN], service)
    assert "was not watching" in await reply(unwatch, ["robinhood", TOKEN], service)
    assert "not watching anything" in await reply(list_command, [], service)


async def test_usage_and_permissions_for_unwatch(session_factory):
    service = make_service(session_factory)
    assert "Usage" in await reply(unwatch, ["robinhood"], service)
    update = make_update(chat_type=ChatType.GROUP)
    assert "administrators" in await reply(unwatch, ["robinhood", TOKEN], service, update)


async def test_any_group_member_can_list(session_factory):
    service = make_service(session_factory)
    update = make_update(chat_type=ChatType.GROUP)
    assert "not watching anything" in await reply(list_command, [], service, update)