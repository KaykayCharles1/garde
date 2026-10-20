from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar

from garde.domain import Chain, CheckReport, RiskEvent, TokenInfo, WatchTarget


@dataclass(frozen=True)
class WatchSnapshot:
    """Facts captured when a watch starts. Addresses are lowercase."""

    token: TokenInfo
    owner: str | None  # None: no owner() function, renounced, or not applicable
    deployer: str | None
    top_wallets: tuple[str, ...]  # largest holders that are wallets, not contracts


class ChainAdapter(ABC):
    """Everything chain-specific lives behind this interface.

    The core never imports web3 or Solana code.
    """

    chain: ClassVar[Chain]

    @abstractmethod
    def normalize_address(self, raw: str) -> str:
        """Validate and canonicalize user input. Raises InvalidAddressError."""

    @abstractmethod
    def explorer_address_url(self, address: str) -> str:
        """Link to this address on the chain's block explorer."""

    @abstractmethod
    async def get_token_info(self, address: str) -> TokenInfo: ...

    @abstractmethod
    async def check_token(self, address: str) -> CheckReport:
        """Static check. Must always include a 'liquidity' finding, visible or not."""

    @abstractmethod
    async def snapshot_for_watch(self, address: str) -> WatchSnapshot:
        """Everything /watch stores. Raises NotATokenError or UpstreamError, never partial."""

    @abstractmethod
    async def poll_events(self, targets: Sequence[WatchTarget]) -> list[RiskEvent]:
        """Return new risk events for the given targets. Scan state is the adapter's business."""