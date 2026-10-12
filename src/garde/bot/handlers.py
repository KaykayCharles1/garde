import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

logger = logging.getLogger(__name__)

START_TEXT = (
    "Garde watches tokens for risky onchain activity and sends risk signals.\n\n"
    "Commands are coming soon: /watch, /unwatch, /list, /check, /alerts, /status.\n\n"
    "Garde reports risk signals only. This is not financial advice, "
    "and alerts can arrive after the damage is done."
)

STUB_COMMANDS = ("help", "watch", "unwatch", "list", "check", "alerts", "status")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:  # Telegram updates can lack a message (e.g. edited posts)
        return
    await update.message.reply_text(START_TEXT)


async def not_implemented(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message is None:
        return
    await update.message.reply_text("This command is not built yet.")


def register_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", start))
    for name in STUB_COMMANDS:
        app.add_handler(CommandHandler(name, not_implemented))