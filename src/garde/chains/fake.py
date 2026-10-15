from collections.abc import Sequence

from garde.chains.addresses import normalize_evm_address
from garde.chains.base import ChainAdapter
from garde.domain import Chain, CheckReport, Finding, RiskEvent, Severity, TokenInfo, WatchTarget


class FakeAdapter(ChainAdapter):
    """In-memory adapter for tests. Proves the interface can be implemented end to end."""

    chain = Chain.ROBINHOOD

    def __init__(self, events: list[RiskEvent] | None = None) -> None:
        self._events = list(events or [])
        self.poll_calls = 0

    def normalize_address(self, raw: str) -> str:
        return normalize_evm_address(raw)

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

    async def poll_events(self, targets: Sequence[WatchTarget]) -> list[RiskEvent]:
        self.poll_calls += 1
        wanted = {t.token_address for t in targets}
        return [e for e in self._events if e.token_address in wanted]