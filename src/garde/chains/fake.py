from collections.abc import Sequence

from garde.chains.addresses import normalize_evm_address
from garde.chains.base import ChainAdapter, WatchSnapshot
from garde.domain import Chain, CheckReport, Finding, RiskEvent, Severity, TokenInfo, WatchTarget

FAKE_DEPLOYER = "0x" + "d1" * 20
FAKE_TOP_WALLETS = ("0x" + "a1" * 20, "0x" + "a2" * 20, "0x" + "a3" * 20)


class FakeAdapter(ChainAdapter):
    """In-memory adapter for tests. Proves the interface can be implemented end to end."""

    chain = Chain.ROBINHOOD

    def __init__(self, events: list[RiskEvent] | None = None) -> None:
        self._events = list(events or [])
        self.poll_calls = 0
        # Tests change these to shape what snapshot_for_watch returns.
        self.owner: str | None = None
        self.deployer: str | None = FAKE_DEPLOYER
        self.top_wallets: tuple[str, ...] = FAKE_TOP_WALLETS
        self.snapshot_error: Exception | None = None

    def normalize_address(self, raw: str) -> str:
        return normalize_evm_address(raw)

    def explorer_address_url(self, address: str) -> str:
        return f"https://explorer.invalid/address/{address}"

    async def get_token_info(self, address: str) -> TokenInfo:
        return TokenInfo(
            chain=self.chain,
            address=self.normalize_address(address),
            name="Fake Token",
            symbol="FAKE",
            decimals=18,
            total_supply=1_000_000,
        )

    async def check_token(self, address: str) -> CheckReport:
        token = await self.get_token_info(address)
        liquidity = Finding(
            key="liquidity",
            summary="Liquidity not visible: a liquidity pull would not be detected.",
            severity=Severity.WARNING,
            visible=False,
        )
        return CheckReport(token=token, findings=[liquidity], blind_spots=["fake adapter"])

    async def snapshot_for_watch(self, address: str) -> WatchSnapshot:
        if self.snapshot_error is not None:
            raise self.snapshot_error
        return WatchSnapshot(
            token=await self.get_token_info(address),
            owner=self.owner,
            deployer=self.deployer,
            top_wallets=tuple(self.top_wallets),
        )

    async def poll_events(self, targets: Sequence[WatchTarget]) -> list[RiskEvent]:
        self.poll_calls += 1
        wanted = {t.token_address for t in targets}
        return [e for e in self._events if e.token_address in wanted]