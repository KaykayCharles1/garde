import asyncio
from collections.abc import Sequence
from typing import Any

from garde.chains.addresses import checksum_address, normalize_evm_address
from garde.chains.base import ChainAdapter
from garde.chains.blockscout import ExplorerApi
from garde.chains.errors import CallReverted, NotATokenError, UpstreamError
from garde.chains.evm_rpc import EvmRpc
from garde.chains.holders import analyze_holders, holders_finding
from garde.domain import (
    Chain,
    CheckReport,
    Finding,
    RiskEvent,
    Severity,
    TokenInfo,
    WatchTarget,
)
from garde.sanitize import clean_optional, clean_text, safe_int, short_address

# EIP-1967 storage slots (see the EIP text to cross-check these constants).
IMPLEMENTATION_SLOT = 0x360894A13BA1A3210667C828492DB98DCA3E2076CC3735A920A3CA505D382BBC
ADMIN_SLOT = 0xB53127684A568B3173AE13B9F8A6016E243E63B6E8EE1178D6A717850B5D6103

BLIND_SPOTS = (
    "Admin roles under names other than owner() (for example access-control roles) "
    "are not checked.",
    "What an owner can do is not analyzed yet.",
    "Proxy detection covers the explorer tag and EIP-1967 slots only, not beacon, "
    "diamond or minimal-clone proxies.",
    "Only the zero address and 0x...dEaD are treated as burn addresses.",
    "Verified source is not an audit.",
    "Holder balances come from the explorer and may lag the chain.",
)


def parse_token(address: str, data: dict[str, Any] | None) -> TokenInfo:
    if data is None:
        raise NotATokenError("This address was not found on the Robinhood Chain explorer.")
    token = data.get("token")
    if not isinstance(token, dict):
        if data.get("is_contract") is True:
            raise NotATokenError("This address is a smart contract that is not a recognized token.")
        raise NotATokenError("This address is a wallet, not a token.")
    token_type = token.get("type")
    if token_type != "ERC-20":
        raise NotATokenError(
            f"Unsupported token type: {clean_text(token_type, 20)}. "
            "Garde supports ERC-20 tokens only."
        )
    return TokenInfo(
        chain=Chain.ROBINHOOD,
        address=address,
        name=clean_optional(token.get("name")),
        symbol=clean_optional(token.get("symbol"), 20),
        decimals=safe_int(token.get("decimals"), maximum=255),
        total_supply=safe_int(token.get("total_supply")),
    )


def address_from_word(word: bytes) -> str | None:
    """Read an address from a 32-byte ABI word. None means the zero address."""
    if len(word) < 32:
        raise ValueError("expected a 32-byte word")
    raw = word[12:32]
    if not any(raw):
        return None
    return checksum_address("0x" + raw.hex())


def verified_finding(data: dict[str, Any]) -> Finding:
    flag = data.get("is_verified")
    if flag is True:
        return Finding(
            key="source_verified",
            summary=(
                "Source code is verified on the explorer, so it can be read. Verified source "
                "is not an audit and does not mean the code is safe."
            ),
        )
    if flag is False:
        return Finding(
            key="source_verified",
            severity=Severity.WARNING,
            summary=(
                "Source code is not verified on the explorer, so the contract's behavior "
                "cannot be inspected."
            ),
        )
    return Finding(
        key="source_verified",
        visible=False,
        summary="The explorer did not say whether the source is verified.",
    )


def proxy_finding(data: dict[str, Any], impl: bytes | None, admin: bytes | None) -> Finding:
    signals: list[str] = []
    proxy_type = data.get("proxy_type")
    if proxy_type:
        signals.append(f"the explorer tags it as a {clean_text(proxy_type, 24)} proxy")
    implementations = data.get("implementations")
    if isinstance(implementations, list) and implementations:
        signals.append(f"the explorer lists {len(implementations)} implementation contract(s)")

    slots_unreadable = False
    for label, word in (("implementation", impl), ("admin", admin)):
        if word is None:
            slots_unreadable = True
            continue
        try:
            target = address_from_word(word)
        except ValueError:
            slots_unreadable = True
            continue
        if target:
            signals.append(f"the EIP-1967 {label} slot is set to {short_address(target)}")

    if signals:
        return Finding(
            key="proxy",
            severity=Severity.WARNING,
            summary=(
                "This looks like a proxy: " + "; ".join(signals) + ". Whoever controls the "
                "upgrade can change the token's code."
            ),
        )
    if slots_unreadable:
        return Finding(
            key="proxy",
            visible=False,
            summary=(
                "Proxy status could not be fully checked because contract storage could not "
                "be read. The explorer shows no proxy tag."
            ),
        )
    return Finding(
        key="proxy",
        summary=(
            "No standard proxy detected: the explorer shows no proxy tag and the EIP-1967 "
            "slots are empty. Beacon, diamond and minimal-clone proxies are not checked."
        ),
    )


async def owner_finding(rpc: EvmRpc, address: str) -> Finding:
    try:
        word = await rpc.call_owner(address)
    except CallReverted:
        return Finding(
            key="owner",
            summary=(
                "No owner() function was found: the call reverted. The token likely has no "
                "standard owner. Admin roles under other names would not be visible."
            ),
        )
    except UpstreamError:
        return Finding(
            key="owner",
            visible=False,
            summary="Ownership could not be checked because the RPC provider was unavailable.",
        )
    if not word:
        return Finding(
            key="owner",
            summary=(
                "owner() returned no data, so no standard owner was found. Admin roles under "
                "other names would not be visible."
            ),
        )
    try:
        owner = address_from_word(word)
    except ValueError:
        return Finding(
            key="owner",
            visible=False,
            summary="owner() returned unexpected data, so ownership could not be determined.",
        )
    if owner is None:
        return Finding(
            key="owner",
            summary=(
                "owner() is the zero address, so ownership appears renounced. Admin roles "
                "under other names would not be visible."
            ),
        )
    return Finding(
        key="owner",
        severity=Severity.WARNING,
        summary=(
            f"The owner is {owner}. An active owner exists. Garde does not yet analyze what "
            "an owner can do, so treat its powers as unknown."
        ),
    )


async def read_slot(rpc: EvmRpc, address: str, slot: int) -> bytes | None:
    try:
        return await rpc.read_storage(address, slot)
    except UpstreamError:
        return None


def liquidity_finding() -> Finding:
    return Finding(
        key="liquidity",
        severity=Severity.WARNING,
        visible=False,
        summary=(
            "Liquidity not visible: Garde cannot yet identify where this token's liquidity "
            "lives, so a liquidity pull would not be detected. A contract holder listed above "
            "may be a pool, but Garde cannot confirm that."
        ),
    )


class RobinhoodAdapter(ChainAdapter):
    chain = Chain.ROBINHOOD

    def __init__(
        self,
        explorer: ExplorerApi,
        rpc: EvmRpc,
        *,
        explorer_url: str,
        warn_pct: float,
        critical_pct: float,
    ) -> None:
        self._explorer = explorer
        self._rpc = rpc
        self._explorer_url = explorer_url.rstrip("/")
        self._warn_pct = warn_pct
        self._critical_pct = critical_pct

    def normalize_address(self, raw: str) -> str:
        return normalize_evm_address(raw)

    def explorer_address_url(self, address: str) -> str:
        return f"{self._explorer_url}/address/{address}"

    async def get_token_info(self, address: str) -> TokenInfo:
        normalized = self.normalize_address(address)
        return parse_token(normalized, await self._explorer.get_address(normalized))

    async def check_token(self, address: str) -> CheckReport:
        normalized = self.normalize_address(address)
        data = await self._explorer.get_address(normalized)  # UpstreamError: whole check fails
        token = parse_token(normalized, data)
        facts = data or {}

        holders, owner, impl, admin = await asyncio.gather(
            self._holders(normalized, token.total_supply),
            owner_finding(self._rpc, normalized),
            read_slot(self._rpc, normalized, IMPLEMENTATION_SLOT),
            read_slot(self._rpc, normalized, ADMIN_SLOT),
        )

        findings: list[Finding] = []
        if facts.get("is_scam") is True:
            findings.append(
                Finding(
                    key="scam_flag",
                    severity=Severity.CRITICAL,
                    summary="The explorer flags this address as a scam. " \
                    "Treat it with extreme caution.",
                )
            )
        findings.append(verified_finding(facts))
        findings.append(proxy_finding(facts, impl, admin))
        findings.append(owner)
        deployer = clean_optional(facts.get("creator_address_hash"), 66)
        if deployer:
            findings.append(Finding(key="deployer", summary=f"Deployed by {deployer}."))
        findings.append(holders)
        findings.append(liquidity_finding())
        return CheckReport(token=token, findings=findings, blind_spots=list(BLIND_SPOTS))

    async def _holders(self, address: str, total_supply: int | None) -> Finding:
        try:
            payload = await self._explorer.get_holders(address)
        except UpstreamError:
            return holders_finding(None, total_supply)
        if payload is None:
            return holders_finding(None, total_supply)
        result = analyze_holders(
            payload, total_supply, warn_pct=self._warn_pct, critical_pct=self._critical_pct
        )
        return holders_finding(result, total_supply)

    async def poll_events(self, targets: Sequence[WatchTarget]) -> list[RiskEvent]:
        raise NotImplementedError("Robinhood event polling arrives in M3")