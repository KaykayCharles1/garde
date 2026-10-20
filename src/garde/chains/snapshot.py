import re
from typing import Any

from garde.chains.holders import BURN_ADDRESSES
from garde.sanitize import safe_int

_ADDRESS_RE = re.compile(r"0x[0-9a-f]{40}")


def top_wallet_holders(payload: dict[str, Any], count: int = 3) -> list[str]:
    """The `count` largest holders that are wallets, lowercase.

    Contracts are skipped: transfers out of a pool or locker are normal activity and would
    spam. Only an explicit `is_contract: false` counts as a wallet, so unknown is skipped.
    Burn addresses, empty balances and malformed entries are skipped too.
    """
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    found: list[tuple[int, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        address = item.get("address")
        if not isinstance(address, dict) or address.get("is_contract") is not False:
            continue
        raw = address.get("hash")
        balance = safe_int(item.get("value"))
        if not isinstance(raw, str) or not balance:
            continue
        lowered = raw.lower()
        if lowered in BURN_ADDRESSES or not _ADDRESS_RE.fullmatch(lowered):
            continue
        found.append((balance, lowered))
    found.sort(reverse=True)
    return [address for _, address in found[:count]]