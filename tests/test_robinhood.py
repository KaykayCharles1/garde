import pytest

from garde.chains.errors import CallReverted, NotATokenError, UpstreamError
from garde.domain import Severity
from tests.fakes import fixture_explorer, fixture_rpc, make_adapter

NAUTCAT = "0xe4d5b1f1a64099c7c38d003ad93196258ae57a03"
BASKET = "0x9e00366e55e10e71e4576abafefe3c847cfb6754"
WALLET = "0x5ae4ac3dee7f774869d116e85b2305535560328b"
ACTIVE_OWNER_WORD = bytes(12) + bytes.fromhex("22" * 20)
PROXY_IMPL_WORD = bytes(12) + bytes.fromhex("11" * 20)


def finding(report, key):
    return next(f for f in report.findings if f.key == key)


def nautcat_adapter():
    explorer, rpc = fixture_explorer("nautcat"), fixture_rpc("nautcat")
    return make_adapter(explorer, rpc), explorer, rpc


# --- real fixtures -----------------------------------------------------------------------


async def test_nautcat_real_fixture():
    adapter, _, _ = nautcat_adapter()
    report = await adapter.check_token(NAUTCAT)
    assert report.token.symbol == "NAUTCAT"
    assert report.token.decimals == 18
    assert report.token.total_supply == 10**27
    assert finding(report, "source_verified").severity is Severity.INFO
    assert "No standard proxy" in finding(report, "proxy").summary
    owner = finding(report, "owner")
    assert "No owner() function" in owner.summary
    assert owner.visible is True
    holders = finding(report, "holders")
    assert "PoolManager" in holders.summary
    assert holders.severity is Severity.INFO  # a verified contract holds ~100%: shown, not scored
    assert finding(report, "deployer")
    assert finding(report, "liquidity").visible is False
    assert report.blind_spots


async def test_basket_real_fixture_owner_is_renounced():
    adapter = make_adapter(fixture_explorer("basket"), fixture_rpc("basket"))
    report = await adapter.check_token(BASKET)
    assert "zero address" in finding(report, "owner").summary
    assert finding(report, "owner").severity is Severity.INFO
    assert "more holders" in finding(report, "holders").summary
    assert finding(report, "liquidity").visible is False


async def test_wallet_address_is_not_a_token():
    adapter = make_adapter(fixture_explorer("unverified"), fixture_rpc("unverified"))
    with pytest.raises(NotATokenError, match="wallet"):
        await adapter.check_token(WALLET)
    with pytest.raises(NotATokenError, match="wallet"):
        await adapter.get_token_info(WALLET)


async def test_get_token_info_matches_fixture():
    adapter, _, _ = nautcat_adapter()
    info = await adapter.get_token_info(NAUTCAT)
    assert info.address == NAUTCAT
    assert info.decimals == 18


# --- derived variants (NOT real chain data: nautcat fixture with one field changed) ---------


async def test_derived_unverified_contract_is_a_warning():
    adapter, explorer, _ = nautcat_adapter()
    explorer.address["is_verified"] = False
    report = await adapter.check_token(NAUTCAT)
    assert finding(report, "source_verified").severity is Severity.WARNING


async def test_derived_proxy_tag_is_a_warning():
    adapter, explorer, _ = nautcat_adapter()
    explorer.address["proxy_type"] = "eip1967"
    proxy = finding(await adapter.check_token(NAUTCAT), "proxy")
    assert proxy.severity is Severity.WARNING
    assert "eip1967" in proxy.summary


async def test_derived_proxy_slot_is_a_warning():
    adapter, _, rpc = nautcat_adapter()
    rpc.impl = PROXY_IMPL_WORD
    proxy = finding(await adapter.check_token(NAUTCAT), "proxy")
    assert proxy.severity is Severity.WARNING
    assert "EIP-1967 implementation slot" in proxy.summary


async def test_derived_active_owner_is_a_warning():
    adapter, _, rpc = nautcat_adapter()
    rpc.owner = ACTIVE_OWNER_WORD
    owner = finding(await adapter.check_token(NAUTCAT), "owner")
    assert owner.severity is Severity.WARNING
    assert "0x" + "22" * 20 in owner.summary


async def test_derived_scam_flag_comes_first_and_is_critical():
    adapter, explorer, _ = nautcat_adapter()
    explorer.address["is_scam"] = True
    report = await adapter.check_token(NAUTCAT)
    assert report.findings[0].key == "scam_flag"
    assert report.findings[0].severity is Severity.CRITICAL


async def test_derived_unsupported_token_type():
    adapter, explorer, _ = nautcat_adapter()
    explorer.address["token"]["type"] = "ERC-721"
    with pytest.raises(NotATokenError, match="Unsupported"):
        await adapter.check_token(NAUTCAT)


async def test_derived_address_unknown_to_explorer():
    adapter, explorer, _ = nautcat_adapter()
    explorer.address = None
    with pytest.raises(NotATokenError, match="not found"):
        await adapter.check_token(NAUTCAT)


# --- hostile or broken data ----------------------------------------------------------------


async def test_hostile_token_name_is_sanitized():
    adapter, explorer, _ = nautcat_adapter()
    explorer.address["token"]["name"] = "SAFE\n\u202e" + "x" * 500
    info = (await adapter.check_token(NAUTCAT)).token
    assert "\n" not in info.name
    assert "\u202e" not in info.name
    assert len(info.name) <= 40


async def test_garbage_numbers_become_unknown_not_crashes():
    adapter, explorer, _ = nautcat_adapter()
    explorer.address["token"]["decimals"] = "999"
    explorer.address["token"]["total_supply"] = "-5"
    report = await adapter.check_token(NAUTCAT)
    assert report.token.decimals is None
    assert report.token.total_supply is None
    assert "total supply unknown" in finding(report, "holders").summary


# --- graceful degradation ----------------------------------------------------------------


async def test_owner_rpc_down_still_produces_a_report():
    adapter, _, rpc = nautcat_adapter()
    rpc.owner = UpstreamError("down")
    report = await adapter.check_token(NAUTCAT)
    assert finding(report, "owner").visible is False
    assert finding(report, "liquidity")


async def test_owner_unexpected_data_is_not_visible():
    adapter, _, rpc = nautcat_adapter()
    rpc.owner = b"\x01\x02"
    assert finding(await adapter.check_token(NAUTCAT), "owner").visible is False


async def test_storage_unreadable_means_proxy_not_visible():
    adapter, _, rpc = nautcat_adapter()
    rpc.impl = UpstreamError("down")
    assert finding(await adapter.check_token(NAUTCAT), "proxy").visible is False


async def test_proxy_signal_survives_unreadable_storage():
    adapter, explorer, rpc = nautcat_adapter()
    explorer.address["proxy_type"] = "eip1967"
    rpc.impl = UpstreamError("down")
    proxy = finding(await adapter.check_token(NAUTCAT), "proxy")
    assert proxy.severity is Severity.WARNING
    assert proxy.visible is True


async def test_holders_unavailable_still_produces_a_report():
    adapter, explorer, _ = nautcat_adapter()
    explorer.holders_error = UpstreamError("down")
    assert finding(await adapter.check_token(NAUTCAT), "holders").visible is False


async def test_explorer_down_fails_the_whole_check():
    adapter, explorer, _ = nautcat_adapter()
    explorer.address_error = UpstreamError("down")
    with pytest.raises(UpstreamError):
        await adapter.check_token(NAUTCAT)


async def test_revert_is_distinct_from_empty_owner_data():
    adapter, _, rpc = nautcat_adapter()
    rpc.owner = CallReverted("reverted")
    reverted = finding(await adapter.check_token(NAUTCAT), "owner").summary
    rpc.owner = b""
    empty = finding(await adapter.check_token(NAUTCAT), "owner").summary
    assert reverted != empty


# --- plumbing ----------------------------------------------------------------------------


async def test_poll_events_is_not_built_until_m3():
    adapter, _, _ = nautcat_adapter()
    with pytest.raises(NotImplementedError):
        await adapter.poll_events([])


def test_explorer_url_is_built_from_the_address():
    adapter, _, _ = nautcat_adapter()
    assert adapter.explorer_address_url(NAUTCAT) == f"https://explorer.invalid/address/{NAUTCAT}"