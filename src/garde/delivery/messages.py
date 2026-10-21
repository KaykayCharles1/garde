"""The text of every message Garde sends. Plain text only: nothing in token data can break it."""

from garde.bot.formatting import DISCLAIMER, ICONS
from garde.domain import Severity
from garde.sanitize import clean_text, short_address

MAX_EXPLANATION = 1000


def _severity(value: str) -> Severity:
    try:
        return Severity(value)
    except ValueError:
        return Severity.INFO


def _name(symbol: str | None, address: str) -> str:
    return clean_text(symbol, 20) if symbol else short_address(address)


def _where(block_number: int | None, explorer_url: str) -> str:
    link = clean_text(explorer_url, 300)
    return f"Block {block_number} · {link}" if block_number is not None else link


def render_alert(
    *,
    severity: str,
    symbol: str | None,
    chain: str,
    token_address: str,
    explanation: str,
    explorer_url: str,
    block_number: int | None,
) -> str:
    level = _severity(severity)
    head = f"{ICONS[level]} {level.value.upper()} · {_name(symbol, token_address)} ({chain})"
    return "\n".join(
        [
            head,
            "",
            clean_text(explanation, MAX_EXPLANATION),
            "",
            _where(block_number, explorer_url),
            "",
            DISCLAIMER,
        ]
    )


def render_retraction(
    *,
    severity: str,
    symbol: str | None,
    chain: str,
    token_address: str,
    explanation: str,
    explorer_url: str,
    block_number: int | None,
) -> str:
    head = f"↩️ Earlier alert retracted · {_name(symbol, token_address)} ({chain})"
    return "\n".join(
        [
            head,
            "",
            "The block that held this event was removed by a chain reorganization, so the "
            "event is not on the chain right now. It may be included again; if it is, Garde "
            "will say so.",
            "",
            "Original alert: " + clean_text(explanation, MAX_EXPLANATION),
            "",
            _where(block_number, explorer_url),
            "",
            DISCLAIMER,
        ]
    )


def render_reconfirmed(
    *,
    severity: str,
    symbol: str | None,
    chain: str,
    token_address: str,
    explanation: str,
    explorer_url: str,
    block_number: int | None,
) -> str:
    head = f"↪️ Alert is back on chain · {_name(symbol, token_address)} ({chain})"
    return "\n".join(
        [
            head,
            "",
            "This event had been retracted after a chain reorganization and has now been "
            "included again.",
            "",
            clean_text(explanation, MAX_EXPLANATION),
            "",
            _where(block_number, explorer_url),
            "",
            DISCLAIMER,
        ]
    )


def render_gap(*, chain: str, count: int, first: int | None, last: int | None) -> str:
    span = f"blocks {first} to {last}" if first is not None and last is not None else "a range"
    plural = "stretch" if count == 1 else "stretches"
    return "\n".join(
        [
            f"⚠️ Garde was offline or fell behind on {chain}.",
            "",
            f"It skipped {count} {plural} of blocks without scanning them. The latest covered "
            f"{span}.",
            "Events inside skipped stretches were not detected, so check your watched tokens' "
            "recent activity on the explorer.",
            "",
            DISCLAIMER,
        ]
    )