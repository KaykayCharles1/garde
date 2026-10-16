"""Holder concentration analysis. Pure functions: no network, easy to test."""

from dataclasses import dataclass, replace
from typing import Any

from garde.domain import Finding, Severity
from garde.sanitize import clean_optional, safe_int, short_address

ZERO_ADDRESS = "0x" + "00" * 20
DEAD_ADDRESS = "0x" + "00" * 18 + "dead"
BURN_ADDRESSES = frozenset({ZERO_ADDRESS, DEAD_ADDRESS})
SEVERITY_ORDER = (Severity.INFO, Severity.WARNING, Severity.CRITICAL)
TOP_N = 5


@dataclass(frozen=True)
class Holder:
    address: str
    name: str | None
    is_contract: bool
    is_verified: bool
    balance: int
    pct: float | None = None


@dataclass(frozen=True)
class HoldersResult:
    holders: tuple[Holder, ...]
    burned_balance: int
    skipped: int
    more_pages: bool
    percentages_available: bool
    inconsistent: bool
    severity: Severity
    warn_pct: float
    critical_pct: float


def _parse_item(item: object) -> Holder | None:
    if not isinstance(item, dict):
        return None
    address = item.get("address")
    if not isinstance(address, dict):
        return None
    raw_hash = address.get("hash")
    balance = safe_int(item.get("value"))
    if not isinstance(raw_hash, str) or balance is None:
        return None
    return Holder(
        address=raw_hash,
        name=clean_optional(address.get("name"), 32),
        is_contract=address.get("is_contract") is True,
        is_verified=address.get("is_verified") is True,
        balance=balance,
    )


def _worst_severity(holders: list[Holder], warn_pct: float, critical_pct: float) -> Severity:
    worst = Severity.INFO
    for holder in holders:
        # Verified contracts are shown but not scored: they are often pools or lockers.
        if holder.pct is None or (holder.is_contract and holder.is_verified):
            continue
        if holder.pct >= critical_pct:
            level = Severity.CRITICAL
        elif holder.pct >= warn_pct:
            level = Severity.WARNING
        else:
            continue
        worst = max(worst, level, key=SEVERITY_ORDER.index)
    return worst


def analyze_holders(
    payload: dict[str, Any],
    total_supply: int | None,
    *,
    warn_pct: float,
    critical_pct: float,
) -> HoldersResult | None:
    """Returns None if the payload is unusable. Malformed single entries are skipped and counted."""
    items = payload.get("items")
    if not isinstance(items, list):
        return None

    rows: list[Holder] = []
    burned = 0
    skipped = 0
    for item in items:
        holder = _parse_item(item)
        if holder is None:
            skipped += 1
        elif holder.address.lower() in BURN_ADDRESSES:
            burned += holder.balance
        else:
            rows.append(holder)
    rows.sort(key=lambda h: h.balance, reverse=True)

    denominator = total_supply - burned if total_supply is not None else None
    inconsistent = False
    if denominator is not None and (denominator <= 0 or (rows and rows[0].balance > denominator)):
        denominator = None  # balances larger than supply: do not trust any percentage
        inconsistent = True

    top = rows[:TOP_N]
    severity = Severity.INFO
    if denominator is not None:
        top = [replace(h, pct=h.balance * 100 / denominator) for h in top]
        severity = _worst_severity(top, warn_pct, critical_pct)

    return HoldersResult(
        holders=tuple(top),
        burned_balance=burned,
        skipped=skipped,
        more_pages=bool(payload.get("next_page_params")),
        percentages_available=denominator is not None,
        inconsistent=inconsistent,
        severity=severity,
        warn_pct=warn_pct,
        critical_pct=critical_pct,
    )


def _label(holder: Holder) -> str:
    if holder.is_contract:
        kind = "verified contract" if holder.is_verified else "unverified contract"
    else:
        kind = "wallet"
    return f"{holder.name or short_address(holder.address)} ({kind})"


def holders_finding(result: HoldersResult | None, total_supply: int | None) -> Finding:
    if result is None:
        return Finding(
            key="holders",
            visible=False,
            summary="Holder data was unavailable or malformed, so concentration was not assessed.",
        )
    if not result.holders:
        return Finding(
            key="holders",
            summary="The explorer returned no holders other than burn addresses.",
        )

    lines: list[str] = []
    if result.percentages_available:
        lines.append("Top holders by balance, as a share of supply excluding burn addresses:")
    elif result.inconsistent:
        lines.append("Top holders by balance (shares unavailable: balances and supply disagree):")
    else:
        lines.append("Top holders by balance (shares unavailable: total supply unknown):")
    for rank, holder in enumerate(result.holders, start=1):
        share = f"{holder.pct:.2f}%" if holder.pct is not None else "n/a"
        lines.append(f"{rank}. {_label(holder)}: {share}")

    if result.burned_balance and total_supply:
        burned_pct = result.burned_balance * 100 / total_supply
        lines.append(f"Burn addresses hold {burned_pct:.2f}% of total supply (excluded above).")
    if any(h.is_contract for h in result.holders):
        lines.append(
            "Contract holders can be pools, lockers or custom contracts, and Garde cannot "
            "tell which. Verified contracts are shown but not scored."
        )
    if result.severity is not Severity.INFO:
        threshold = (
            result.critical_pct if result.severity is Severity.CRITICAL else result.warn_pct
        )
        lines.append(
            f"At least one scored holder is at or above the {threshold:g}% "
            f"{result.severity.value} threshold."
        )
    if result.more_pages:
        lines.append("The explorer has more holders than the first page Garde reads.")
    if result.skipped:
        lines.append(f"{result.skipped} malformed holder entries were ignored.")
    return Finding(key="holders", severity=result.severity, summary="\n".join(lines))