"""Sends queued notices to Telegram.

Rules: never hold a database lock while talking to Telegram, send at least once (a crash can
cause a duplicate, never a loss), and let one broken chat or message never block the queue.
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Protocol

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from telegram.error import (
    BadRequest,
    ChatMigrated,
    Forbidden,
    NetworkError,
    RetryAfter,
    TelegramError,
)

from garde.db import event_repo, scan_repo
from garde.db.models import AlertEvent, Chat, ScanState, Token, Watch
from garde.delivery import messages

logger = logging.getLogger(__name__)


class Messenger(Protocol):
    """Where messages go. Tests substitute a fake that can raise Telegram errors."""

    async def send(self, chat_id: int, text: str) -> None: ...


class TelegramMessenger:
    def __init__(self, bot) -> None:
        self._bot = bot

    async def send(self, chat_id: int, text: str) -> None:
        await self._bot.send_message(chat_id=chat_id, text=text)


@dataclass(frozen=True)
class SenderConfig:
    poll_seconds: float = 2.0
    batch_size: int = 100
    pause_between_sends: float = 0.05  # keeps us under Telegram's global message rate
    max_attempts: int = 5
    retraction_window: timedelta = timedelta(hours=24)
    backoff_base: float = 1.0
    backoff_max: float = 60.0


@dataclass
class PassReport:
    sent: int = 0
    dropped: int = 0
    gap_notices: int = 0
    flood_wait: float | None = None  # Telegram told us to slow down
    network_problem: bool = False


class Result(Enum):
    SENT = "sent"
    RETRY = "retry"  # keep the notice and try again later
    DROP = "drop"  # this one message can never be delivered: forget it
    CHAT_GONE = "chat_gone"  # the chat blocked the bot or removed it


def seconds_of(value: float | timedelta) -> float:
    """RetryAfter.retry_after is an int today and may become a timedelta: accept both."""
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


class DeliverySender:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        messenger: Messenger,
        config: SenderConfig | None = None,
        *,
        chain: str = "robinhood",
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._messenger = messenger
        self._cfg = config or SenderConfig()
        self._chain = chain
        self._sleep = sleep
        self._now = now
        self._attempts: defaultdict[int, int] = defaultdict(int)

    # ---- the loop ---------------------------------------------------------------------

    async def run_forever(self) -> None:
        failures = 0
        while True:
            delay = self._cfg.poll_seconds
            try:
                report = await self.run_once()
            except Exception:  # a bug must not silently kill the sender
                failures += 1
                logger.exception("sender_unexpected_error failures=%d", failures)
                delay = self._backoff(failures)
            else:
                if report.network_problem:
                    failures += 1
                    delay = self._backoff(failures)
                else:
                    failures = 0
                    if report.flood_wait is not None:
                        delay = max(delay, report.flood_wait)
            await self._sleep(delay)

    def _backoff(self, failures: int) -> float:
        return min(self._cfg.backoff_max, self._cfg.backoff_base * 2 ** (failures - 1))

    # ---- one pass ---------------------------------------------------------------------

    async def run_once(self) -> PassReport:
        report = PassReport()
        await self._finalize_retractions()
        await self._send_pending(report)
        await self._send_gap_notices(report)
        return report

    async def _finalize_retractions(self) -> None:
        async with self._session_factory() as session, session.begin():
            tip = await scan_repo.load_tip(session, self._chain)
            if tip is not None:
                await event_repo.finalize_retractions(
                    session,
                    self._chain,
                    tip.height,
                    since=self._now() - self._cfg.retraction_window,
                )

    async def _send_pending(self, report: PassReport) -> None:
        async with self._session_factory() as session:
            pending = await event_repo.pending_deliveries(session, self._cfg.batch_size)
            delivery_ids = [delivery.id for delivery, _ in pending]
        for delivery_id in delivery_ids:
            if report.flood_wait is not None or report.network_problem:
                break
            await self._deliver(delivery_id, report)
            await self._sleep(self._cfg.pause_between_sends)

    async def _deliver(self, delivery_id: int, report: PassReport) -> None:
        async with self._session_factory() as session:
            # Re-check right before sending: the chat may have unwatched, or the event been
            # retracted, since the pass began.
            pending = await event_repo.pending_deliveries(session, 1, delivery_id=delivery_id)
            if not pending:
                return
            delivery, event = pending[0]
            chat = await session.get(Chat, delivery.chat_id)
            token = await session.get(Token, event.token_id)
            if chat is None or token is None:
                return
            text = self._render(delivery.kind, event, token)
            telegram_chat_id, chat_id = chat.telegram_chat_id, chat.id
        result = await self._attempt(telegram_chat_id, text, report)
        await self._settle(delivery_id, chat_id, result, report)

    @staticmethod
    def _render(kind: str, event: AlertEvent, token: Token) -> str:
        fields = {
            "severity": event.severity,
            "symbol": token.symbol,
            "chain": token.chain,
            "token_address": token.address,
            "explanation": event.explanation,
            "explorer_url": event.explorer_url,
            "block_number": event.block_number,
        }
        if kind == "retraction":
            return messages.render_retraction(**fields)
        if kind == "reconfirmed":
            return messages.render_reconfirmed(**fields)
        return messages.render_alert(**fields)

    # ---- talking to Telegram ----------------------------------------------------------

    async def _attempt(self, telegram_chat_id: int, text: str, report: PassReport) -> Result:
        try:
            await self._messenger.send(telegram_chat_id, text)
        except RetryAfter as exc:
            report.flood_wait = seconds_of(exc.retry_after)
            logger.warning("send_flood_wait seconds=%.0f", report.flood_wait)
            return Result.RETRY
        except ChatMigrated as exc:
            return await self._after_migration(telegram_chat_id, exc.new_chat_id, text, report)
        except Forbidden:
            return Result.CHAT_GONE
        except BadRequest:  # a NetworkError subclass, so it must come before NetworkError
            logger.warning("send_rejected")  # the text is never logged
            return Result.DROP
        except NetworkError:  # includes TimedOut
            report.network_problem = True
            return Result.RETRY
        except TelegramError as exc:
            logger.warning("send_failed error=%s", type(exc).__name__)
            return Result.RETRY
        return Result.SENT

    async def _after_migration(
        self, old_id: int, new_id: int, text: str, report: PassReport
    ) -> Result:
        """A group became a supergroup: follow it to its new id and resend."""
        try:
            async with self._session_factory() as session, session.begin():
                await session.execute(
                    update(Chat).where(Chat.telegram_chat_id == old_id).values(telegram_chat_id=new_id)
                )
        except IntegrityError:
            logger.warning("chat_migration_conflict")  # the new id already has its own chat row
            return Result.CHAT_GONE
        logger.info("chat_migrated")
        return await self._attempt(new_id, text, report)

    # ---- bookkeeping after an attempt -------------------------------------------------

    async def _settle(
        self, delivery_id: int, chat_id: int, result: Result, report: PassReport
    ) -> None:
        if result is Result.SENT:
            async with self._session_factory() as session, session.begin():
                await event_repo.mark_sent(session, delivery_id)
            self._attempts.pop(delivery_id, None)
            report.sent += 1
        elif result is Result.DROP:
            await self._forget_delivery(delivery_id)
            report.dropped += 1
        elif result is Result.CHAT_GONE:
            await self._drop_chat(chat_id)
            report.dropped += 1
        elif report.flood_wait is None and not report.network_problem:
            self._attempts[delivery_id] += 1
            if self._attempts[delivery_id] >= self._cfg.max_attempts:
                logger.error("delivery_given_up id=%d", delivery_id)
                await self._forget_delivery(delivery_id)
                report.dropped += 1

    async def _forget_delivery(self, delivery_id: int) -> None:
        async with self._session_factory() as session, session.begin():
            await event_repo.delete_delivery(session, delivery_id)
        self._attempts.pop(delivery_id, None)

    async def _drop_chat(self, chat_id: int) -> None:
        """The chat can no longer receive messages: remove it, its watches and its queue."""
        async with self._session_factory() as session, session.begin():
            await session.execute(delete(Chat).where(Chat.id == chat_id))  # cascades
            await session.execute(delete(Token).where(Token.id.not_in(select(Watch.token_id))))
        logger.warning("chat_dropped")

    # ---- gap notices ------------------------------------------------------------------

    async def _send_gap_notices(self, report: PassReport) -> None:
        async with self._session_factory() as session:
            state = await session.get(ScanState, self._chain)
            if state is None or not state.gaps_total:
                return
            total, first, last = state.gaps_total, state.last_gap_first, state.last_gap_last
            watching = (
                select(Watch.chat_id)
                .join(Token, Watch.token_id == Token.id)
                .where(Token.chain == self._chain)
            )
            rows = (
                await session.execute(
                    select(Chat.id, Chat.telegram_chat_id, Chat.gaps_notified).where(
                        Chat.gaps_notified < total, Chat.id.in_(watching)
                    )
                )
            ).all()
        for chat_id, telegram_chat_id, notified in rows:
            if report.flood_wait is not None or report.network_problem:
                break
            text = messages.render_gap(
                chain=self._chain, count=total - notified, first=first, last=last
            )
            result = await self._attempt(telegram_chat_id, text, report)
            if result in (Result.SENT, Result.DROP):
                async with self._session_factory() as session, session.begin():
                    await session.execute(
                        update(Chat).where(Chat.id == chat_id).values(gaps_notified=total)
                    )
                report.gap_notices += 1 if result is Result.SENT else 0
            elif result is Result.CHAT_GONE:
                await self._drop_chat(chat_id)
            await self._sleep(self._cfg.pause_between_sends)