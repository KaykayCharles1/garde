import re

from web3 import Web3

from garde.chains.errors import InvalidAddressError

_EVM_RE = re.compile(r"0x[0-9a-fA-F]{40}")
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"  #pragma: allowlist secret


def normalize_evm_address(raw: str) -> str:
    candidate = raw.strip()
    if not _EVM_RE.fullmatch(candidate):
        raise InvalidAddressError(f"not a valid EVM address: {raw!r}")
    hex_part = candidate[2:]
    # All-lowercase or all-uppercase carries no checksum. Mixed case claims one, so verify it.
    is_mixed_case = hex_part != hex_part.lower() and hex_part != hex_part.upper()
    if is_mixed_case and not Web3.is_checksum_address(candidate):
        raise InvalidAddressError("EVM address has an invalid checksum (check for typos)")
    return candidate.lower()


def checksum_address(address: str) -> str:
    return Web3.to_checksum_address(address)


def validate_solana_address(raw: str) -> str:
    """A Solana address is the base58 form of exactly 32 bytes. Case is significant."""
    candidate = raw.strip()
    if not candidate:
        raise InvalidAddressError("empty Solana address")
    number = 0
    for char in candidate:
        index = _B58_ALPHABET.find(char)
        if index == -1:
            raise InvalidAddressError(f"invalid base58 character {char!r}")
        number = number * 58 + index
    leading_zero_bytes = len(candidate) - len(candidate.lstrip("1"))
    body_bytes = (number.bit_length() + 7) // 8
    if leading_zero_bytes + body_bytes != 32:
        raise InvalidAddressError("Solana address does not decode to 32 bytes")
    return candidate