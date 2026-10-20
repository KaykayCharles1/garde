class InvalidAddressError(ValueError):
    """Raised when user input is not a valid address for the chain."""


class NotATokenError(Exception):
    """The address is not a token Garde can check. The message is safe to show users."""


class UpstreamError(Exception):
    """A data provider failed. The message must never contain URLs or API keys."""


class RpcRejected(UpstreamError):
    """The node answered with a JSON-RPC error (for example 'range too large')."""


class RpcRateLimited(UpstreamError):
    """The node answered HTTP 429. Could be a request budget or a query that was too heavy."""


class RpcLagging(UpstreamError):
    """The node does not have a block it should have: it is behind or inconsistent."""


class ChainChangedDuringScan(UpstreamError):
    """The chain reorganized while a chunk was being scanned. Nothing was committed."""


class CallReverted(Exception):
    """An eth_call reverted (for example, the contract has no such function)."""