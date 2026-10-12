from unittest.mock import AsyncMock, MagicMock

from garde.bot.handlers import start


async def test_start_replies_with_disclaimer():
    update = MagicMock()
    update.message.reply_text = AsyncMock()
    await start(update, MagicMock())
    update.message.reply_text.assert_awaited_once()
    text = update.message.reply_text.await_args.args[0]
    assert "not financial advice" in text.lower()