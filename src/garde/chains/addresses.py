import re

from garde.chains.errors import InvalidAddressError

_EVM_RE = re.compile(r"0x[0-9a-fA-F]{40}")
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"  #pragma: allowlist secret


def normalize_evm_address(raw: str) -> str:
    candidate = raw.strip()
    if not _EVM_RE.fullmatch(candidate):
        raise InvalidAddressError(f"not a valid EVM address: {raw!r}")
    return candidate.lower()


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