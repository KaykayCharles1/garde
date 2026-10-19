"""Turn raw EVM logs into risk events. Pure functions: no network, no database.

What we decode are a contract's own announcements (events). A contract can emit an event
without the matching state change, so these are claims, not proof.
"""

from collections.abc import Callable
from dataclasses import dataclass, field

from web3 import Web3

from garde.chains.holders import BURN_ADDRESSES, ZERO_ADDRESS
from garde.chains.scan_rpc import RawLog
from garde.domain import Chain, RiskEvent, Rule, Severity
from garde.sanitize import clean_text, short_address


def event_topic(signature: str) -> str:
    """topic0 is keccak256 of the canonical signature: types only, never parameter names."""
    return "0x" + bytes(Web3.keccak(text=signature)).hex()


TRANSFER = event_topic("Transfer(address,address,uint256)")
OWNERSHIP_TRANSFERRED = event_topic("OwnershipTransferred(address,address)")
UPGRADED = event_topic("Upgraded(address)")
ADMIN_CHANGED = event_topic("AdminChanged(address,address)")
PAUSED = event_topic("Paused(address)")
UNPAUSED = event_topic("Unpaused(address)")

EVENT_TOPICS = (OWNERSHIP_TRANSFERRED, UPGRADED, ADMIN_CHANGED, PAUSED, UNPAUSED)


@dataclass(frozen=True)
class TokenContext:
    address: str  # lowercase
    symbol: str | None = None
    decimals: int | None = None
    supply: int | None = None  # raw units, snapshot taken when the watch started
    wallets: dict[str, str] = field(default_factory=dict)  # lowercase address -> label


@dataclass(frozen=True)
class Thresholds:
    warning_pct: float = 1.0
    critical_pct: float = 5.0


@dataclass(frozen=True)
class Detection:
    event: RiskEvent
    wallet: str | None = None  # set for wallet-scoped events; None means token-wide
    new_owner: str | None = None  # set for ownership events (zero address = renounced)


@dataclass(frozen=True)
class _Outcome:
    rule: Rule
    severity: Severity
    text: str
    wallet: str | None = None
    new_owner: str | None = None


def _word_address(word: str) -> str | None:
    """Address inside a 32-byte hex word. None unless the top 12 bytes are zero padding."""
    if len(word) != 66 or word[2:26] != "0" * 24:
        return None
    return "0x" + word[26:].lower()


def _data_words(data: str) -> list[str] | None:
    body = data[2:].lower()
    if len(body) % 64:
        return None
    return ["0x" + body[i : i + 64] for i in range(0, len(body), 64)]


def _share(amount: int, supply: int | None) -> float | None:
    return amount * 100 / supply if supply else None


def format_amount(raw: int, decimals: int | None, symbol: str | None) -> str:
    unit = f" {clean_text(symbol, 20)}" if symbol else ""
    if decimals is None:
        return f"{raw:,} base units{unit}"
    whole, fraction = divmod(raw, 10**decimals)
    digits = f"{fraction:0{decimals}d}"[:4].rstrip("0") if decimals else ""
    return f"{whole:,}{'.' + digits if digits else ''}{unit}"


def _decode_transfer(log: RawLog) -> tuple[str, str, int] | None:
    """ERC-20 Transfer: three topics and one data word. ERC-721 has four topics: not ours."""
    if len(log.topics) != 3:
        return None
    words = _data_words(log.data)
    if words is None or len(words) != 1:
        return None
    sender = _word_address(log.topics[1])
    receiver = _word_address(log.topics[2])
    if sender is None or receiver is None:
        return None
    return sender, receiver, int(words[0], 16)


def _transfer(log: RawLog, ctx: TokenContext, limits: Thresholds) -> _Outcome | None:
    decoded = _decode_transfer(log)
    if decoded is None:
        return None
    sender, receiver, amount = decoded
    if amount == 0:
        return None
    share = _share(amount, ctx.supply)
    amount_text = format_amount(amount, ctx.decimals, ctx.symbol)

    if sender == ZERO_ADDRESS:  # a mint
        if receiver == ZERO_ADDRESS:
            return None
        severity = Severity.WARNING
        if share is not None and share >= limits.critical_pct:
            severity = Severity.CRITICAL
        size = f" ({share:.2f}% of the supply recorded when the watch started)" if share else ""
        text = f"New supply was minted: {amount_text}{size} to {short_address(receiver)}."
        return _Outcome(Rule.SUPPLY_MINTED, severity, text)

    label = ctx.wallets.get(sender)
    if label is None or share is None or share < limits.warning_pct:
        return None
    who = f"{clean_text(label, 24)} wallet {short_address(sender)}"
    size = f"({share:.2f}% of the supply recorded when the watch started)"
    if receiver in BURN_ADDRESSES:
        text = f"{who} burned {amount_text} {size} by sending it to a burn address."
        return _Outcome(Rule.LARGE_TRANSFER_OUT, Severity.INFO, text, wallet=sender)
    severity = Severity.CRITICAL if share >= limits.critical_pct else Severity.WARNING
    text = f"{who} sent {amount_text} {size} to {short_address(receiver)}."
    return _Outcome(Rule.LARGE_TRANSFER_OUT, severity, text, wallet=sender)


def _ownership(log: RawLog, ctx: TokenContext, limits: Thresholds) -> _Outcome | None:
    if len(log.topics) != 3:
        return None
    previous = _word_address(log.topics[1])
    new = _word_address(log.topics[2])
    if previous is None or new is None:
        return None
    if new == ZERO_ADDRESS:
        text = (
            "Ownership was renounced: the owner is now the zero address. "
            "Admin powers under other names, if any, are not visible."
        )
        return _Outcome(Rule.OWNERSHIP_RENOUNCED, Severity.INFO, text, new_owner=new)
    text = f"Ownership moved from {short_address(previous)} to {short_address(new)}."
    return _Outcome(Rule.OWNERSHIP_TRANSFERRED, Severity.WARNING, text, new_owner=new)


def _upgraded(log: RawLog, ctx: TokenContext, limits: Thresholds) -> _Outcome | None:
    if len(log.topics) != 2:
        return None
    implementation = _word_address(log.topics[1])
    if implementation is None:
        return None
    text = (
        f"The implementation behind this proxy changed to {short_address(implementation)}. "
        "The token's code may now behave differently."
    )
    return _Outcome(Rule.PROXY_UPGRADED, Severity.CRITICAL, text)


def _admin_changed(log: RawLog, ctx: TokenContext, limits: Thresholds) -> _Outcome | None:
    words = _data_words(log.data)
    if len(log.topics) != 1 or words is None or len(words) != 2:
        return None
    previous, new = _word_address(words[0]), _word_address(words[1])
    if previous is None or new is None:
        return None
    text = f"The proxy admin changed from {short_address(previous)} to {short_address(new)}."
    return _Outcome(Rule.ADMIN_CHANGED, Severity.WARNING, text)


def _pause_account(log: RawLog) -> str | None | bool:
    """The account named by Paused/Unpaused; None if absent; False if the log is malformed."""
    words = _data_words(log.data)
    if len(log.topics) == 1:
        if words is None or len(words) > 1:
            return False
        return _word_address(words[0]) if words else None
    if len(log.topics) == 2 and not words:
        return _word_address(log.topics[1]) or False
    return False


def _paused(log: RawLog, ctx: TokenContext, limits: Thresholds) -> _Outcome | None:
    account = _pause_account(log)
    if account is False:
        return None
    by = f" by {short_address(account)}" if account else ""
    text = (
        f"The token contract was paused{by}. "
        "Depending on its code, transfers may be blocked until it is unpaused."
    )
    return _Outcome(Rule.PAUSED, Severity.CRITICAL, text)


def _unpaused(log: RawLog, ctx: TokenContext, limits: Thresholds) -> _Outcome | None:
    account = _pause_account(log)
    if account is False:
        return None
    by = f" by {short_address(account)}" if account else ""
    return _Outcome(Rule.UNPAUSED, Severity.INFO, f"The token contract was unpaused{by}.")


_HANDLERS: dict[str, Callable[[RawLog, TokenContext, Thresholds], _Outcome | None]] = {
    TRANSFER: _transfer,
    OWNERSHIP_TRANSFERRED: _ownership,
    UPGRADED: _upgraded,
    ADMIN_CHANGED: _admin_changed,
    PAUSED: _paused,
    UNPAUSED: _unpaused,
}


def detect(
    log: RawLog,
    ctx: TokenContext,
    *,
    chain: Chain,
    explorer_url: str,
    thresholds: Thresholds,
) -> list[Detection]:
    """Zero or one detections for a log. Anything unrecognized or malformed is ignored."""
    if log.address != ctx.address or not log.topics:
        return []
    handler = _HANDLERS.get(log.topics[0])
    if handler is None:
        return []
    outcome = handler(log, ctx, thresholds)
    if outcome is None:
        return []
    event = RiskEvent(
        chain=chain,
        token_address=ctx.address,
        rule=outcome.rule,
        severity=outcome.severity,
        tx_id=log.tx_hash,
        log_index=log.log_index,
        explanation=outcome.text,
        explorer_url=f"{explorer_url.rstrip('/')}/tx/{log.tx_hash}",
        block_number=log.block_number,
    )
    return [Detection(event=event, wallet=outcome.wallet, new_owner=outcome.new_owner)]