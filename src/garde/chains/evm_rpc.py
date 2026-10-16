from typing import Protocol

from web3 import AsyncWeb3, Web3
from web3.exceptions import ContractLogicError

from garde.chains.errors import CallReverted, UpstreamError

OWNER_SELECTOR = "0x8da5cb5b"  # first 4 bytes of keccak256("owner()")


class EvmRpc(Protocol):
    """The only two RPC reads M2 needs. Tests substitute a fake."""

    async def call_owner(self, address: str) -> bytes:
        """Raw bytes returned by owner(). Raises CallReverted if the call reverts."""

    async def read_storage(self, address: str, slot: int) -> bytes: ...


class Web3Rpc:
    """AsyncWeb3 behind the EvmRpc interface. Everything web3-specific stays in this file."""

    def __init__(self, rpc_url: str) -> None:
        self._w3 = AsyncWeb3(AsyncWeb3.AsyncHTTPProvider(rpc_url))

    async def call_owner(self, address: str) -> bytes:
        target = Web3.to_checksum_address(address)
        try:
            result = await self._w3.eth.call({"to": target, "data": OWNER_SELECTOR})
        except ContractLogicError as exc:
            raise CallReverted("owner() reverted") from exc
        except Exception as exc:  # network boundary: provider errors vary by library version
            raise UpstreamError(f"rpc failure ({type(exc).__name__})") from None
        return bytes(result)

    async def read_storage(self, address: str, slot: int) -> bytes:
        target = Web3.to_checksum_address(address)
        try:
            value = await self._w3.eth.get_storage_at(target, slot)
        except Exception as exc:  # network boundary
            raise UpstreamError(f"rpc failure ({type(exc).__name__})") from None
        return bytes(value)