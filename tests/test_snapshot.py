import pytest

from garde.chains.errors import NotATokenError, UpstreamError
from garde.chains.holders import BURN_ADDRESSES
from garde.chains.snapshot import top_wallet_holders
from tests.fakes import fixture_explorer, fixture_rpc, load_fixture, make_adapter

NAUTCAT = "0xe4d5b1f1a64099c7c38d003ad93196258ae57a03"
BASKET = "0x9e00366e55e10e71e4576abafefe3c847cfb6754"
WALLET = "0x5ae4ac3dee7f774869d116e85b2305535560328b"


def entry(address, value, *, contract=False):
    return {"address": {"hash": address, "is_contract": contract}, "value": str(value)}


def a(n: int) -> str:
    return "0x" + f"{n:040x}"


def test_only_wallets_are_returned_largest_first_and_limited():
    items = [
        entry(a(1), 10),
        entry(a(2), 500, contract=True),  # a pool: skipped
        entry(a(3), 300),
        entry(a(4), 200),
        entry(a(5), 100),
    ]
    result = top_wallet_holders({"items": items}, 3)
    assert result == [a(3), a(4), a(5)]
    assert a(1) not in result  # the smallest wallet falls off the limit of 3
    assert a(2) not in result  # the contract is never included, whatever its size


def test_burn_zero_unknown_and_malformed_entries_are_skipped():
    items = [
        entry("0x" + "00" * 20, 999),
        entry("0x" + "00" * 18 + "dEaD", 999),  # burn address, mixed case
        entry(a(1), 0),
        {"address": {"hash": a(2)}, "value": "50"},  # is_contract missing: unknown, skipped
        {"address": {"hash": "0xnothex", "is_contract": False}, "value": "50"},
        {"address": {"hash": a(3), "is_contract": False}, "value": "abc"},
        "junk",
        None,
        entry(a(9), 7),
    ]
    assert top_wallet_holders({"items": items}) == [a(9)]


def test_addresses_come_back_lowercase():
    upper = "0x" + "AB" * 20
    assert top_wallet_holders({"items": [entry(upper, 5)]}) == ["0x" + "ab" * 20]


@pytest.mark.parametrize("payload", [{}, {"items": None}, {"items": "nope"}])
def test_unusable_payloads_give_no_wallets(payload):
    assert top_wallet_holders(payload) == []


# --- real fixtures ------------------------------------------------------------------------


def expected_wallets(name: str, count: int = 3) -> list[str]:
    """An independent reading of the real fixture (it is sorted by balance, which we verified)."""
    out = []
    for item in load_fixture(name, "holders")["body"]["items"]:
        address = item["address"]
        if (
            address["is_contract"] is False
            and address["hash"].lower() not in BURN_ADDRESSES
            and int(item["value"]) > 0
        ):
            out.append(address["hash"].lower())
    return out[:count]


def creator(name: str) -> str:
    return load_fixture(name, "address")["body"]["creator_address_hash"].lower()


async def test_nautcat_snapshot_has_a_deployer_but_no_owner_and_no_wallet_holders():
    adapter = make_adapter(fixture_explorer("nautcat"), fixture_rpc("nautcat"))
    snapshot = await adapter.snapshot_for_watch(NAUTCAT)
    assert snapshot.token.symbol == "NAUTCAT"
    assert snapshot.owner is None  # owner() reverted
    assert snapshot.deployer == creator("nautcat")
    assert list(snapshot.top_wallets) == expected_wallets("nautcat")


async def test_basket_snapshot_has_three_wallet_holders_and_a_renounced_owner():
    adapter = make_adapter(fixture_explorer("basket"), fixture_rpc("basket"))
    snapshot = await adapter.snapshot_for_watch(BASKET)
    assert snapshot.owner is None  # owner() returned the zero address
    assert snapshot.deployer == creator("basket")
    assert list(snapshot.top_wallets) == expected_wallets("basket")
    assert len(snapshot.top_wallets) == 3


async def test_a_wallet_address_cannot_be_watched_as_a_token():
    adapter = make_adapter(fixture_explorer("unverified"), fixture_rpc("unverified"))
    with pytest.raises(NotATokenError):
        await adapter.snapshot_for_watch(WALLET)


async def test_an_active_owner_is_captured_lowercase():
    rpc = fixture_rpc("nautcat")
    rpc.owner = bytes(12) + bytes.fromhex("22" * 20)
    adapter = make_adapter(fixture_explorer("nautcat"), rpc)
    assert (await adapter.snapshot_for_watch(NAUTCAT)).owner == "0x" + "22" * 20


async def test_provider_failures_fail_the_snapshot_instead_of_degrading_it():
    rpc = fixture_rpc("nautcat")
    rpc.owner = UpstreamError("rpc down")
    with pytest.raises(UpstreamError):
        await make_adapter(fixture_explorer("nautcat"), rpc).snapshot_for_watch(NAUTCAT)

    explorer = fixture_explorer("nautcat")
    explorer.holders_error = UpstreamError("explorer down")
    with pytest.raises(UpstreamError):
        await make_adapter(explorer, fixture_rpc("nautcat")).snapshot_for_watch(NAUTCAT)