import pytest

from garde.chains.holders import analyze_holders, holders_finding
from garde.domain import Severity

WALLET_A = "0x" + "aa" * 20
WALLET_B = "0x" + "bb" * 20
WALLET_C = "0x" + "cc" * 20
ZERO = "0x" + "00" * 20
DEAD_MIXED_CASE = "0x000000000000000000000000000000000000dEaD"


def holder(address, value, *, contract=False, verified=False, name=None):
    return {
        "address": {
            "hash": address,
            "is_contract": contract,
            "is_verified": verified,
            "name": name,
        },
        "token_id": None,
        "value": str(value),
    }


def payload(*items, more=False):
    return {"items": list(items), "next_page_params": {"page": 2} if more else None}


def analyze(data, supply):
    return analyze_holders(data, supply, warn_pct=20.0, critical_pct=50.0)


def test_burn_addresses_are_excluded_from_the_denominator():
    result = analyze(payload(holder(ZERO, 500), holder(WALLET_A, 300)), 1000)
    assert result.burned_balance == 500
    assert result.holders[0].pct == 60.0
    assert result.severity is Severity.CRITICAL


def test_dead_address_matches_in_any_letter_case():
    result = analyze(payload(holder(DEAD_MIXED_CASE, 400), holder(WALLET_A, 100)), 1000)
    assert result.burned_balance == 400
    assert len(result.holders) == 1


@pytest.mark.parametrize(
    ("balance", "expected"),
    [(19, Severity.INFO), (20, Severity.WARNING), (49, Severity.WARNING), (50, Severity.CRITICAL)],
)
def test_wallet_thresholds_are_inclusive(balance, expected):
    assert analyze(payload(holder(WALLET_A, balance)), 100).severity is expected


def test_verified_contract_is_shown_but_not_scored():
    data = payload(holder(WALLET_A, 90, contract=True, verified=True, name="PoolManager"))
    result = analyze(data, 100)
    assert result.holders[0].pct == 90.0
    assert result.severity is Severity.INFO


def test_unverified_contract_is_scored():
    result = analyze(payload(holder(WALLET_A, 90, contract=True, verified=False)), 100)
    assert result.severity is Severity.CRITICAL


def test_holders_are_sorted_and_limited_to_five():
    items = [holder("0x" + f"{i:02x}" * 20, i) for i in range(1, 9)]  # ascending on purpose
    result = analyze(payload(*items), 1000)
    assert len(result.holders) == 5
    assert result.holders[0].balance == 8


def test_malformed_entries_are_skipped_and_counted():
    items = [
        None,
        "x",
        {"address": None, "value": "1"},
        holder(WALLET_A, "abc"),
        {"address": {"hash": WALLET_B}, "value": None},
        holder(WALLET_C, 10),
    ]
    result = analyze(payload(*items), 100)
    assert result.skipped == 5
    assert [h.address for h in result.holders] == [WALLET_C]


def test_balances_larger_than_supply_disable_percentages():
    result = analyze(payload(holder(WALLET_A, 500)), 100)
    assert result.inconsistent is True
    assert result.percentages_available is False
    assert result.severity is Severity.INFO


def test_unknown_supply_disables_percentages():
    result = analyze(payload(holder(WALLET_A, 5)), None)
    assert result.percentages_available is False
    assert "total supply unknown" in holders_finding(result, None).summary


@pytest.mark.parametrize("bad", [{}, {"items": "nope"}, {"items": None}])
def test_unusable_payload_returns_none_and_a_not_visible_finding(bad):
    assert analyze(bad, 100) is None
    finding = holders_finding(None, 100)
    assert finding.visible is False


def test_more_pages_is_disclosed():
    result = analyze(payload(holder(WALLET_A, 1), more=True), 100)
    assert "more holders" in holders_finding(result, 100).summary


def test_hostile_holder_names_are_sanitized():
    hostile = "Evil\nName\u202e " + "x" * 200
    result = analyze(payload(holder(WALLET_A, 10, contract=True, verified=True, name=hostile)), 100)
    name = result.holders[0].name
    assert "\n" not in name
    assert "\u202e" not in name
    assert len(name) <= 32