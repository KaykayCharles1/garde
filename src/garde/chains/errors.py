class InvalidAddressError(ValueError):
    """Raised when user input is not a valid address for the chain."""


class NotATokenError(Exception):
    """The address is not a token Garde can check. The message is safe to show users."""


class UpstreamError(Exception):
    """A data provider failed. The message must never contain URLs or API keys."""


class CallReverted(Exception):
    """An eth_call reverted (for example, the contract has no such function)."""