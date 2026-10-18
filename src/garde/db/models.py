from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from garde.db.base import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(16))  # "private" or "group"
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Token(Base):
    """One row per (chain, address), shared by every chat that watches it."""

    __tablename__ = "tokens"
    __table_args__ = (UniqueConstraint("chain", "address", name="uq_tokens_chain_address"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(16))
    address: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Watch(Base):
    __tablename__ = "watches"
    __table_args__ = (UniqueConstraint("chat_id", "token_id", name="uq_watches_chat_token"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"))
    disabled_rules: Mapped[list[str]] = mapped_column(JSON, default=list)  # for /alerts
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    # lazy="selectin" loads wallets together with the watch. Async sessions cannot do lazy loading.
    wallets: Mapped[list["WatchedWallet"]] = relationship(
        cascade="all, delete-orphan", lazy="selectin"
    )


class WatchedWallet(Base):
    __tablename__ = "watched_wallets"
    __table_args__ = (
        UniqueConstraint("watch_id", "address", name="uq_watched_wallets_watch_address"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"))
    address: Mapped[str] = mapped_column(String(64))
    label: Mapped[str | None] = mapped_column(String(32), default=None)


class ProcessedBlock(Base):
    """Checkpoints: (height, hash) of blocks the scanner has fully processed.

    The scan cursor is the highest height. Old rows are pruned past the checkpoint window.
    """

    __tablename__ = "processed_blocks"

    chain: Mapped[str] = mapped_column(String(16), primary_key=True)
    height: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    block_hash: Mapped[str] = mapped_column(String(80))


class ScanState(Base):
    """Operational state of one chain scanner, for /status and for noticing problems."""

    __tablename__ = "scan_state"

    chain: Mapped[str] = mapped_column(String(16), primary_key=True)
    last_head: Mapped[int | None] = mapped_column(BigInteger, default=None)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    last_error: Mapped[str | None] = mapped_column(String(200), default=None)
    reorgs_total: Mapped[int] = mapped_column(Integer, default=0)
    deep_reorgs_total: Mapped[int] = mapped_column(Integer, default=0)
    last_reorg_rewind_blocks: Mapped[int | None] = mapped_column(Integer, default=None)
    last_reorg_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    gaps_total: Mapped[int] = mapped_column(Integer, default=0)
    last_gap_first: Mapped[int | None] = mapped_column(BigInteger, default=None)
    last_gap_last: Mapped[int | None] = mapped_column(BigInteger, default=None)


class AlertEvent(Base):
    __tablename__ = "alert_events"
    __table_args__ = (
        UniqueConstraint("chain", "tx_id", "log_index", "rule", name="uq_alert_events_dedup"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chain: Mapped[str] = mapped_column(String(16))
    tx_id: Mapped[str] = mapped_column(String(100))
    log_index: Mapped[int] = mapped_column(Integer)
    rule: Mapped[str] = mapped_column(String(48))
    token_id: Mapped[int] = mapped_column(ForeignKey("tokens.id", ondelete="CASCADE"))
    severity: Mapped[str] = mapped_column(String(16))
    explanation: Mapped[str] = mapped_column(Text)
    explorer_url: Mapped[str] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Delivery(Base):
    """One row per (event, chat). sent_at stays NULL until Telegram accepts the message."""

    __tablename__ = "deliveries"
    __table_args__ = (
        UniqueConstraint("alert_event_id", "chat_id", name="uq_deliveries_event_chat"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_event_id: Mapped[int] = mapped_column(ForeignKey("alert_events.id", ondelete="CASCADE"))
    chat_id: Mapped[int] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)