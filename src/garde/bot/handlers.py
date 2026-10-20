import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from garde.bot.formatting import format_report
from garde.bot.watch_handlers import list_command, unwatch, watch
from garde.chains.errors import InvalidAddressError, NotATokenError, UpstreamError
from garde.domain import Chain

logger = logging.getLogger(__name__)

START_TEXT = (
    "Garde watches tokens for risky onchain activity and sends risk signals.\n\n"
    "Try: /check robinhood <token address>\n"
    "Watch a token: /watch robinhood <token address>\n"
    "Also: /unwatch, /list. Coming soon: /alerts, /status.\n\n"
    "Garde reports risk signals only. This is not financial advice, "
    "and alerts can arrive after the damage is done."
)
USAGE = "Usage: /check <chain> <address>\nExample: /check robinhood 0x..."
STUB_COMMANDS = ("help", "alerts", "status")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:  # Telegram updates can lack a message (e.g. edited posts)
        return
    await update.message.reply_text(START_TEXT)


async def not_implemented(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text("This command is not built yet.")


async def check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None:
        return
    args = context.args or []
    if len(args) != 2:
        await message.reply_text(USAGE)
        return

    try:
        chain = Chain(args[0].lower())
    except ValueError:
        supported = ", ".join(c.value for c in Chain)
        await message.reply_text(f"Unknown chain. Supported chains: {supported}.")
        return
    adapter = context.application.bot_data.get("adapters", {}).get(chain)
    if adapter is None:
        await message.reply_text(f"{chain.value} is not supported yet.")
        return

    try:
        address = adapter.normalize_address(args[1])
    except InvalidAddressError:
        await message.reply_text(f"That is not a valid {chain.value} address.")
        return

    try:
        report = await adapter.check_token(address)
    except NotATokenError as exc:
        await message.reply_text(str(exc))
    except UpstreamError:
        logger.warning("check_upstream_failed chain=%s", chain.value)
        await message.reply_text(
            "Data providers are unavailable right now. Please try again in a minute."
        )
    except Exception:
        logger.exception("check_failed chain=%s", chain.value)
        await message.reply_text("Something went wrong while checking that token.")
    else:
        await message.reply_text(format_report(report, adapter.explorer_address_url(address)))


def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("check", check))
    app.add_handler(CommandHandler("watch", watch))
    app.add_handler(CommandHandler("unwatch", unwatch))
    app.add_handler(CommandHandler("list", list_command))
    for name in STUB_COMMANDS:
        app.add_handler(CommandHandler(name, not_implemented))