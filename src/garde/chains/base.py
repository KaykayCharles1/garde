from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import ClassVar

from garde.domain import Chain, CheckReport, RiskEvent, TokenInfo, WatchTarget


class ChainAdapter(ABC):
    """Everything chain-specific lives behind this interface.
    The core never imports web3 or Solana code.
    """

    chain: ClassVar[Chain]

    @abstractmethod
    def normalize_address(self, raw: str) -> str:
        """Validate and canonicalize user input. Raises InvalidAddressError."""

    @abstractmethod
    async def get_token_info(self, address: str) -> TokenInfo: ...

    @abstractmethod
    async def check_token(self, address: str) -> CheckReport:
        """Static check. Must always include a 'liquidity' finding, visible or not."""

    @abstractmethod
    async def poll_events(self, targets: Sequence[WatchTarget]) -> list[RiskEvent]:
        """Return new risk events for the given targets. Scan state is the adapter's business."""