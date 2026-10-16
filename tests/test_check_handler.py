from unittest.mock import AsyncMock, MagicMock

from garde.bot.handlers import check
from garde.chains.errors import NotATokenError, UpstreamError
from garde.chains.fake import FakeAdapter
from garde.domain import Chain

TOKEN = "0x" + "ab" * 20


class FailingAdapter(FakeAdapter):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self._error = error

    async def check_token(self, address):
        raise self._error


def make_update():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    return update


def make_context(args, adapter=None):
    context = MagicMock()
    context.args = args
    context.application.bot_data = {"adapters": {Chain.ROBINHOOD: adapter or FakeAdapter()}}
    return context


async def reply_to(args, adapter=None) -> str:
    update = make_update()
    await check(update, make_context(args, adapter))
    update.message.reply_text.assert_awaited_once()
    return update.message.reply_text.await_args.args[0]


async def test_wrong_argument_count_shows_usage():
    assert "Usage" in await reply_to([])
    assert "Usage" in await reply_to(["robinhood"])


async def test_unknown_chain():
    assert "Unknown chain" in await reply_to(["dogechain", TOKEN])


async def test_known_chain_without_adapter():
    assert "not supported yet" in await reply_to(["solana", TOKEN])


async def test_invalid_address():
    assert "not a valid" in await reply_to(["robinhood", "0x123"])


async def test_success_reply_contains_report_and_disclaimer():
    text = await reply_to(["robinhood", TOKEN])
    assert "FAKE" in text
    assert "not financial advice" in text


async def test_not_a_token_message_is_passed_through():
    text = await reply_to(["robinhood", TOKEN], FailingAdapter(NotATokenError("a wallet")))
    assert text == "a wallet"


async def test_upstream_failure_is_friendly_and_hides_details():
    text = await reply_to(["robinhood", TOKEN], FailingAdapter(UpstreamError("secret detail")))
    assert "unavailable" in text
    assert "secret detail" not in text


async def test_unexpected_failure_is_generic_and_hides_details():
    text = await reply_to(["robinhood", TOKEN], FailingAdapter(RuntimeError("internal boom")))
    assert "went wrong" in text
    assert "internal boom" not in text