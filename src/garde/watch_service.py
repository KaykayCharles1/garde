"""Creating, removing and listing watches. Each public method that writes is one transaction."""

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from garde.chains.base import ChainAdapter, WatchSnapshot
from garde.chains.errors import InvalidAddressError
from garde.chains.holders import BURN_ADDRESSES
from garde.db.models import AlertEvent, Chat, Delivery, Token, Watch, WatchedWallet
from garde.domain import Chain
from garde.sanitize import clean_text

logger = logging.getLogger(__name__)


class WatchError(Exception):
    """A problem the user can fix. The message is safe to show them."""


class LimitReached(WatchError):
    """A per-chat or global cap was hit."""


@dataclass(frozen=True)
class ServiceConfig:
    max_per_chat: int
    max_global: int
    confirmations: int
    large_transfer_pct: float  # only used to describe the rule to the user
    max_extra_wallets: int = 10
    max_wallets_per_watch: int = 15


@dataclass(frozen=True)
class AddResult:
    chain: Chain
    address: str
    symbol: str | None
    owner: str | None
    wallets: tuple[tuple[str, str], ...]  # (address, label)
    already_watching: bool
    new_wallets: int
    start_block: int


@dataclass(frozen=True)
class WatchSummary:
    chain: str
    address: str
    symbol: str | None
    wallet_count: int


async def _take_write_lock(session: AsyncSession) -> None:
    """Take SQLite's write lock before reading anything a decision depends on.

    In SQLite's default mode a transaction only begins at the first write, so a plain
    "read the count, then insert" lets two commands read the same count. This no-op UPDATE
    begins the transaction first: a second command waits here until the first commits.
    """
    await session.execute(text("UPDATE chats SET id = id WHERE 0"))


async def _count(session: AsyncSession, model, *criteria) -> int:
    stmt = select(func.count()).select_from(model)
    if criteria:
        stmt = stmt.where(*criteria)
    return (await session.execute(stmt)).scalar_one()


async def _ensure_chat(session: AsyncSession, telegram_chat_id: int, kind: str) -> Chat:
    result = await session.execute(select(Chat).where(Chat.telegram_chat_id == telegram_chat_id))
    chat = result.scalar_one_or_none()
    if chat is None:
        chat = Chat(telegram_chat_id=telegram_chat_id, kind=kind)
        session.add(chat)
        await session.flush()
    return chat


async def _find_token(session: AsyncSession, chain: Chain, address: str) -> Token | None:
    result = await session.execute(
        select(Token).where(Token.chain == chain.value, Token.address == address)
    )
    return result.scalar_one_or_none()


class WatchService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        adapters: dict[Chain, ChainAdapter],
        head_providers: dict[Chain, Callable[[], Awaitable[int]]],
        config: ServiceConfig,
    ) -> None:
        self._session_factory = session_factory
        self._adapters = adapters
        self._head_providers = head_providers
        self._cfg = config

    @property
    def config(self) -> ServiceConfig:
        return self._cfg

    def _adapter(self, chain: Chain) -> ChainAdapter:
        adapter = self._adapters.get(chain)
        if adapter is None:
            raise WatchError(f"{chain.value} is not supported yet.")
        return adapter

    # ---- add ---------------------------------------------------------------------------

    async def add_watch(
        self,
        *,
        telegram_chat_id: int,
        chat_kind: str,
        chain: Chain,
        address: str,
        extra_wallets: Sequence[tuple[str, str]] = (),
    ) -> AddResult:
        adapter = self._adapter(chain)
        head_provider = self._head_providers.get(chain)
        if head_provider is None:
            raise WatchError(f"{chain.value} is not supported yet.")
        token_address = adapter.normalize_address(address)  # InvalidAddressError propagates
        extras = self._clean_extras(adapter, chain, token_address, extra_wallets)

        # Network work first, outside any database transaction.
        snapshot = await adapter.snapshot_for_watch(token_address)
        start_block = max(await head_provider() - self._cfg.confirmations, 0)
        wallets = self._assemble_wallets(snapshot, extras, token_address)

        async with self._session_factory() as session, session.begin():
            await _take_write_lock(session)
            chat = await _ensure_chat(session, telegram_chat_id, chat_kind)
            token = await _find_token(session, chain, token_address)
            if token is not None:
                existing = await session.execute(
                    select(Watch).where(Watch.chat_id == chat.id, Watch.token_id == token.id)
                )
                watch = existing.scalar_one_or_none()
                if watch is not None:
                    return self._extend_watch(chain, token, watch, extras)

            watching = await _count(session, Watch, Watch.chat_id == chat.id)
            if watching >= self._cfg.max_per_chat:
                raise LimitReached(
                    f"This chat already watches {watching} tokens, which is the limit. "
                    "Use /unwatch to free a slot."
                )
            if token is None:
                total = await _count(session, Token)
                if total >= self._cfg.max_global:
                    raise LimitReached(
                        f"Garde is at capacity ({total} tokens watched in total). "
                        "Please try again later."
                    )
                supply = snapshot.token.total_supply
                token = Token(
                    chain=chain.value,
                    address=token_address,
                    symbol=snapshot.token.symbol,
                    decimals=snapshot.token.decimals,
                    supply_snapshot=None if supply is None else str(supply),
                    owner_address=snapshot.owner,
                )
                session.add(token)
                await session.flush()
            session.add(
                Watch(
                    chat_id=chat.id,
                    token_id=token.id,
                    start_block=start_block,
                    disabled_rules=[],
                    wallets=[WatchedWallet(address=a, label=lbl) for a, lbl in wallets],
                )
            )
            result = AddResult(
                chain=chain,
                address=token_address,
                symbol=token.symbol,
                owner=token.owner_address,
                wallets=tuple(wallets),
                already_watching=False,
                new_wallets=len(wallets),
                start_block=start_block,
            )
        logger.info(
            "watch_added chain=%s token=%s wallets=%d start=%d",
            chain.value, token_address, len(wallets), start_block,
        )
        return result

    def _extend_watch(
        self, chain: Chain, token: Token, watch: Watch, extras: list[tuple[str, str]]
    ) -> AddResult:
        have = {w.address: w.label or "wallet" for w in watch.wallets}
        fresh = [(address, label) for address, label in extras if address not in have]
        if len(have) + len(fresh) > self._cfg.max_wallets_per_watch:
            raise WatchError(
                f"A watch can hold at most {self._cfg.max_wallets_per_watch} wallets."
            )
        for address, label in fresh:
            watch.wallets.append(WatchedWallet(address=address, label=label))
        return AddResult(
            chain=chain,
            address=token.address,
            symbol=token.symbol,
            owner=token.owner_address,
            wallets=(*have.items(), *fresh),
            already_watching=True,
            new_wallets=len(fresh),
            start_block=watch.start_block,
        )

    def _clean_extras(
        self,
        adapter: ChainAdapter,
        chain: Chain,
        token_address: str,
        extras: Sequence[tuple[str, str]],
    ) -> list[tuple[str, str]]:
        cleaned: dict[str, str] = {}
        for label, raw in extras:
            try:
                address = adapter.normalize_address(raw)
            except InvalidAddressError:
                raise WatchError(
                    f"'{clean_text(raw, 20)}' is not a valid {chain.value} address."
                ) from None
            if address == token_address:
                raise WatchError("The token's own address cannot be a watched wallet.")
            if address in BURN_ADDRESSES:
                raise WatchError("Burn addresses cannot be watched wallets.")
            cleaned[address] = clean_text(label, 24) or "wallet"
        if len(cleaned) > self._cfg.max_extra_wallets:
            raise WatchError(f"At most {self._cfg.max_extra_wallets} extra wallets per watch.")
        return list(cleaned.items())

    @staticmethod
    def _assemble_wallets(
        snapshot: WatchSnapshot, extras: list[tuple[str, str]], token_address: str
    ) -> list[tuple[str, str]]:
        merged: dict[str, str] = {}
        if snapshot.deployer:
            merged[snapshot.deployer] = "deployer"
        for wallet in snapshot.top_wallets:
            merged.setdefault(wallet, "top holder")
        for address, label in extras:
            merged[address] = label  # a label the user chose wins over an automatic one
        return [
            (address, label)
            for address, label in merged.items()
            if address not in BURN_ADDRESSES and address != token_address
        ]

    # ---- remove and list -----------------------------------------------------------------

    async def remove_watch(self, *, telegram_chat_id: int, chain: Chain, address: str) -> bool:
        token_address = self._adapter(chain).normalize_address(address)
        async with self._session_factory() as session, session.begin():
            await _take_write_lock(session)
            chat = (
                await session.execute(
                    select(Chat).where(Chat.telegram_chat_id == telegram_chat_id)
                )
            ).scalar_one_or_none()
            token = await _find_token(session, chain, token_address)
            if chat is None or token is None:
                return False
            watch = (
                await session.execute(
                    select(Watch).where(Watch.chat_id == chat.id, Watch.token_id == token.id)
                )
            ).scalar_one_or_none()
            if watch is None:
                return False

            # Notices still waiting to be sent for this chat and token must not go out.
            event_ids = select(AlertEvent.id).where(AlertEvent.token_id == token.id)
            await session.execute(
                delete(Delivery).where(
                    Delivery.chat_id == chat.id,
                    Delivery.sent_at.is_(None),
                    Delivery.alert_event_id.in_(event_ids),
                )
            )
            await session.delete(watch)
            await session.flush()
            if await _count(session, Watch, Watch.token_id == token.id) == 0:
                await session.delete(token)  # last watcher gone: its events cascade away
        logger.info("watch_removed chain=%s token=%s", chain.value, token_address)
        return True

    async def list_watches(self, *, telegram_chat_id: int) -> list[WatchSummary]:
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(Watch, Token)
                    .join(Token, Watch.token_id == Token.id)
                    .join(Chat, Watch.chat_id == Chat.id)
                    .where(Chat.telegram_chat_id == telegram_chat_id)
                    .order_by(Watch.id)
                )
            ).all()
        return [
            WatchSummary(
                chain=token.chain,
                address=token.address,
                symbol=token.symbol,
                wallet_count=len(watch.wallets),
            )
            for watch, token in rows
        ]