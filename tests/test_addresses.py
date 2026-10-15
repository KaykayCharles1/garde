import pytest

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