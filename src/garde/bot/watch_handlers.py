import logging

from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from garde.bot.formatting import DISCLAIMER
from garde.chains.errors import InvalidAddressError, NotATokenError, UpstreamError
from garde.domain import Chain
from garde.sanitize import clean_text, short_address
from garde.watch_service import AddResult, WatchError, WatchService

logger = logging.getLogger(__name__)

WATCH_USAGE = (
    "Usage: /watch <chain> <address> [label=wallet ...]\n"
    "Example: /watch robinhood 0xTOKEN treasury=0xWALLET"
)
UNWATCH_USAGE = "Usage: /unwatch <chain> <address>"
ADMINS_ONLY = "Only chat administrators can change this group's watches."
UNAVAILABLE = "Data providers are unavailable right now. Please try again in a minute."
FAILED = "Something went wrong. Please try again."


def _parse_chain(value: str) -> Chain | None:
    try:
        return Chain(value.lower())
    except ValueError:
        return None


def _parse_wallet_args(args: list[str]) -> list[tuple[str, str]]:
    """'treasury=0x...' becomes ('treasury', '0x...'); a bare address gets the label 'wallet'."""
    wallets = []
    for arg in args:
        label, separator, address = arg.partition("=")
        wallets.append((label, address) if separator else ("wallet", arg))
    return wallets


def _service(context: ContextTypes.DEFAULT_TYPE) -> WatchService | None:
    return context.application.bot_data.get("watch_service")


async def may_edit_watches(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Private chats: always. Groups: administrators only, anonymous ones included."""
    chat, message = update.effective_chat, update.message
    if chat is None or message is None:
        return False
    if chat.type == ChatType.PRIVATE:
        return True
    sender_chat = message.sender_chat
    if sender_chat is not None and sender_chat.id == chat.id:
        return True  # an anonymous group admin posts on behalf of the group itself
    user = update.effective_user
    if user is None:
        return False
    member = await context.bot.get_chat_member(chat.id, user.id)
    return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)


async def _permission_granted(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Replies to the user when permission is denied or cannot be checked."""
    message = update.message
    try:
        allowed = await may_edit_watches(update, context)
    except TelegramError:
        logger.warning("permission_check_failed")
        await message.reply_text("Could not check your permissions right now. Please try again.")
        return False
    if not allowed:
        await message.reply_text(ADMINS_ONLY)
    return allowed


def format_added(result: AddResult, large_pct: float) -> str:
    name = clean_text(result.symbol, 20) if result.symbol else short_address(result.address)
    lines: list[str] = []
    if result.already_watching:
        lines.append(f"Already watching {name} on {result.chain.value}.")
        if result.new_wallets:
            lines.append(f"Added {result.new_wallets} wallet(s) to the watch.")
    else:
        lines.append(f"Now watching {name} on {result.chain.value}.")
        lines.append(
            "You will get risk signals for: ownership changes, proxy upgrades, pause or "
            f"unpause, new supply minted, and transfers of {large_pct:g}% or more of the "
            "supply out of watched wallets. Events before this moment are not reported."
        )
    lines += ["", "Watched wallets (a snapshot taken when the watch started):"]
    if not result.wallets:
        lines.append("- none found")
    for address, label in result.wallets:
        lines.append(f"- {clean_text(label, 24)}: {short_address(address)}")
    if result.owner:
        lines.append(f"- owner (followed automatically): {short_address(result.owner)}")
    else:
        lines.append("- owner: none found (no owner() function, or ownership renounced)")
    lines += ["", DISCLAIMER]
    return "\n".join(lines)


async def watch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, chat = update.message, update.effective_chat
    if message is None or chat is None:
        return
    args = context.args or []
    if len(args) < 2:
        await message.reply_text(WATCH_USAGE)
        return
    chain = _parse_chain(args[0])
    if chain is None:
        supported = ", ".join(c.value for c in Chain)
        await message.reply_text(f"Unknown chain. Supported chains: {supported}.")
        return
    service = _service(context)
    if service is None:
        await message.reply_text("Watching is not available right now.")
        return
    if not await _permission_granted(update, context):
        return

    kind = "private" if chat.type == ChatType.PRIVATE else "group"
    try:
        result = await service.add_watch(
            telegram_chat_id=chat.id,
            chat_kind=kind,
            chain=chain,
            address=args[1],
            extra_wallets=_parse_wallet_args(args[2:]),
        )
    except InvalidAddressError:
        await message.reply_text(f"That is not a valid {chain.value} address.")
    except (WatchError, NotATokenError) as exc:
        await message.reply_text(str(exc))
    except UpstreamError:
        logger.warning("watch_upstream_failed chain=%s", chain.value)
        await message.reply_text(UNAVAILABLE)
    except Exception:
        logger.exception("watch_failed chain=%s", chain.value)
        await message.reply_text(FAILED)
    else:
        await message.reply_text(format_added(result, service.config.large_transfer_pct))


async def unwatch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, chat = update.message, update.effective_chat
    if message is None or chat is None:
        return
    args = context.args or []
    if len(args) != 2:
        await message.reply_text(UNWATCH_USAGE)
        return
    chain = _parse_chain(args[0])
    if chain is None:
        await message.reply_text("Unknown chain.")
        return
    service = _service(context)
    if service is None:
        await message.reply_text("Watching is not available right now.")
        return
    if not await _permission_granted(update, context):
        return
    try:
        removed = await service.remove_watch(
            telegram_chat_id=chat.id, chain=chain, address=args[1]
        )
    except InvalidAddressError:
        await message.reply_text(f"That is not a valid {chain.value} address.")
    except WatchError as exc:
        await message.reply_text(str(exc))
    except Exception:
        logger.exception("unwatch_failed chain=%s", chain.value)
        await message.reply_text(FAILED)
    else:
        if removed:
            await message.reply_text("Stopped watching that token. Queued notices were dropped.")
        else:
            await message.reply_text("This chat was not watching that token.")


async def list_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message, chat = update.message, update.effective_chat
    if message is None or chat is None:
        return
    service = _service(context)
    if service is None:
        await message.reply_text("Watching is not available right now.")
        return
    try:
        summaries = await service.list_watches(telegram_chat_id=chat.id)
    except Exception:
        logger.exception("list_failed")
        await message.reply_text(FAILED)
        return
    if not summaries:
        await message.reply_text(
            "This chat is not watching anything yet.\nTry: /watch robinhood <token address>"
        )
        return
    lines = [f"Watching {len(summaries)} of {service.config.max_per_chat} tokens:"]
    for number, item in enumerate(summaries, start=1):
        name = clean_text(item.symbol, 20) if item.symbol else short_address(item.address)
        lines.append(
            f"{number}. {name} ({short_address(item.address)}) on {item.chain}, "
            f"{item.wallet_count} wallets"
        )
    await message.reply_text("\n".join(lines))