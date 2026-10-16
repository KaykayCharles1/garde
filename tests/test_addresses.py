import pytest
from web3 import Web3

from garde.chains.addresses import normalize_evm_address, validate_solana_address
from garde.chains.errors import InvalidAddressError

B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"   #pragma: allowlist secret


def b58encode(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    out = ""
    while number:
        number, rem = divmod(number, 58)
        out = B58[rem] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def test_evm_is_lowercased():
    assert normalize_evm_address("0x" + "AB" * 20) == "0x" + "ab" * 20


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "ab" * 20,
        "0x" + "ab" * 19,
        "0x" + "ab" * 21,
        "0x" + "zz" * 20,
        " 0x ",
        "0x" + "ab" * 20 + "\n0x",
    ],
)
def test_evm_rejects_bad_input(bad):
    with pytest.raises(InvalidAddressError):
        normalize_evm_address(bad)


def test_solana_accepts_32_byte_keys_and_keeps_case():
    for data in (bytes(range(32)), bytes([1]) * 32, bytes(32)):
        address = b58encode(data)
        assert validate_solana_address(address) == address


@pytest.mark.parametrize("size", [0, 31, 33])
def test_solana_rejects_wrong_length(size):
    with pytest.raises(InvalidAddressError):
        validate_solana_address(b58encode(bytes([7]) * size) or "1" * size)


@pytest.mark.parametrize("bad", ["", "0OIl" * 8, "not base58!"])
def test_solana_rejects_bad_characters(bad):
    with pytest.raises(InvalidAddressError):
        validate_solana_address(bad)

def test_valid_checksum_is_accepted_and_lowercased():
    lower = "0xe4d5b1f1a64099c7c38d003ad93196258ae57a03"
    assert normalize_evm_address(Web3.to_checksum_address(lower)) == lower


def test_mixed_case_with_a_wrong_checksum_is_rejected():
    good = Web3.to_checksum_address("0xe4d5b1f1a64099c7c38d003ad93196258ae57a03")
    i = next(n for n, ch in enumerate(good) if n >= 2 and ch.isalpha())
    flipped = good[:i] + good[i].swapcase() + good[i + 1 :]
    with pytest.raises(InvalidAddressError, match="checksum"):
        normalize_evm_address(flipped)