"""Scanner hooks: turn logs into stored risk events and one delivery row per chat."""

import logging
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from garde.chains.holders import ZERO_ADDRESS
from garde.chains.scan_rpc import RawLog
from garde.db import event_repo
from garde.db.models import Token, Watch
from garde.domain import Chain
from garde.events.decoders import (
    EVENT_TOPICS,
    TRANSFER,
    Detection,
    Thresholds,
    TokenContext,
    detect,
)
from garde.sanitize import safe_int

logger = logging.getLogger(__name__)


@dataclass
class WatchView:
    chat_id: int
    start_block: int
    disabled_rules: set[str]
    wallets: dict[str, str]  # lowercase address -> label


@dataclass
class TokenView:
    token_id: int
    address: str
    symbol: str | None
    decimals: int | None
    supply: int | None
    owner: str | None
    watches: list[WatchView] = field(default_factory=list)

    def wallets(self) -> dict[str, str]:
        """Every wallet worth decoding transfers for: all watches' wallets plus the owner."""
        merged: dict[str, str] = {}
        for watch in self.watches:
            for address, label in watch.wallets.items():
                merged.setdefault(address, label)
        if self.owner:
            merged[self.owner] = "owner"
        return merged

    def context(self) -> TokenContext:
        return TokenContext(
            address=self.address,
            symbol=self.symbol,
            decimals=self.decimals,
            supply=self.supply,
            wallets=self.wallets(),
        )


async def load_views(session: AsyncSession, chain: str) -> dict[str, TokenView]:
    rows = (
        await session.execute(
            select(Watch, Token).join(Token, Watch.token_id == Token.id).where(Token.chain == chain)
        )
    ).all()
    views: dict[str, TokenView] = {}
    for watch, token in rows:
        view = views.get(token.address)
        if view is None:
            view = TokenView(
                token_id=token.id,
                address=token.address,
                symbol=token.symbol,
                decimals=token.decimals,
                supply=safe_int(token.supply_snapshot),
                owner=token.owner_address,
            )
            views[token.address] = view
        view.watches.append(
            WatchView(
                chat_id=watch.chat_id,
                start_block=watch.start_block,
                disabled_rules=set(watch.disabled_rules or []),
                wallets={w.address: (w.label or "watched") for w in watch.wallets},
            )
        )
    return views


def _padded(address: str) -> str:
    return "0x" + "00" * 12 + address[2:]


def _chunks(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield list(items[start : start + size])


def build_filters(views: Iterable[TokenView], *, limit: int) -> list[dict[str, Any]]:
    """eth_getLogs filters: contract events, plus transfers from watched wallets and mints.

    Mints are transfers from the zero address, so it joins the wallet list. Wallets and
    tokens are crossed, so a few extra transfers can match; the decoder narrows them down.
    Every list is split into pieces of at most `limit` items.
    """
    views = list(views)
    addresses = sorted({view.address for view in views})
    if not addresses:
        return []
    wallets = {ZERO_ADDRESS}
    for view in views:
        wallets.update(view.wallets())
    wallet_topics = sorted(_padded(wallet) for wallet in wallets)

    filters: list[dict[str, Any]] = []
    for address_chunk in _chunks(addresses, limit):
        filters.append({"address": address_chunk, "topics": [list(EVENT_TOPICS)]})
        for topic_chunk in _chunks(wallet_topics, limit):
            filters.append({"address": address_chunk, "topics": [[TRANSFER], topic_chunk]})
    return filters


class EventHooks:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        chain: Chain,
        explorer_url: str,
        thresholds: Thresholds,
        max_filter_items: int = 500,
    ) -> None:
        self._session_factory = session_factory
        self._chain = chain
        self._explorer_url = explorer_url
        self._thresholds = thresholds
        self._limit = max_filter_items

    async def get_filters(self) -> list[dict[str, Any]]:
        async with self._session_factory() as session:
            views = await load_views(session, self._chain.value)
        return build_filters(views.values(), limit=self._limit)

    async def on_logs(
        self, session: AsyncSession, logs: list[RawLog], from_block: int, to_block: int
    ) -> int | None:
        views = await load_views(session, self._chain.value)
        created = 0
        for log in logs:  # the scanner delivers logs ordered by (block, log index)
            view = views.get(log.address)
            if view is None:
                continue
            detections = detect(
                log,
                view.context(),
                chain=self._chain,
                explorer_url=self._explorer_url,
                thresholds=self._thresholds,
            )
            for detection in detections:
                chats = self._recipients(view, detection, log.block_number)
                if chats:
                    event_id, outcome = await event_repo.store_event(
                        session, detection.event, view.token_id, log.block_hash
                    )
                    if outcome == "new":
                        await event_repo.queue_deliveries(session, event_id, chats, "alert")
                        created += 1
                owner_changed = detection.new_owner is not None and await self._apply_owner(
                    session, view, detection.new_owner
                )
                if owner_changed:
                    # The watched wallets changed. End the chunk just before this block so the
                    # next one is fetched with fresh filters; this block replays harmlessly.
                    return log.block_number - 1
        if created:
            logger.info("events_created chain=%s count=%d", self._chain.value, created)
        return None

    async def on_reorg(self, session: AsyncSession, fork_height: int, old_tip: int) -> None:
        retracted = await event_repo.mark_retracted_above(
            session, self._chain.value, fork_height
        )
        logger.warning(
            "events_retracted chain=%s fork=%d count=%d", self._chain.value, fork_height, retracted
        )

    @staticmethod
    def _recipients(view: TokenView, detection: Detection, block_number: int) -> list[int]:
        rule = detection.event.rule.value
        wallet = detection.wallet
        chats: list[int] = []
        for watch in view.watches:
            if block_number <= watch.start_block or rule in watch.disabled_rules:
                continue
            # Wallet events go to the watches that listed the wallet; the owner is for everyone.
            if wallet is not None and wallet != view.owner and wallet not in watch.wallets:
                continue
            chats.append(watch.chat_id)
        return chats

    @staticmethod
    async def _apply_owner(session: AsyncSession, view: TokenView, new_owner: str) -> bool:
        """Record the new owner (None if renounced). Returns True if it actually changed."""
        owner = None if new_owner == ZERO_ADDRESS else new_owner
        if owner == view.owner:
            return False
        view.owner = owner
        await session.execute(
            update(Token).where(Token.id == view.token_id).values(owner_address=owner)
        )
        return True