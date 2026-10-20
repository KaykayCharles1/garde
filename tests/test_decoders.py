import json

import pytest

from garde.chains.holders import ZERO_ADDRESS
from garde.chains.scan_rpc import RawLog, parse_log
from garde.domain import Chain, Rule, Severity
from garde.events.decoders import (
    ADMIN_CHANGED,
    OWNERSHIP_TRANSFERRED,
    PAUSED,
    TRANSFER,
    UNPAUSED,
    UPGRADED,
    Thresholds,
    TokenContext,
    detect,
    format_amount,
)
from garde.sanitize import short_address
from tests.fakes import FIXTURES

TOKEN = "0x" + "aa" * 20
WALLET = "0x" + "bb" * 20
OTHER = "0x" + "cc" * 20
DEAD = "0x" + "00" * 18 + "dead"
TX = "0x" + "dd" * 32
EXPLORER = "https://explorer.invalid"
LIMITS = Thresholds(warning_pct=1.0, critical_pct=5.0)


def pad(address: str) -> str:
    return "0x" + "00" * 12 + address[2:]


def word(value: int) -> str:
    return f"{value:064x}"


def make_log(topics, data="0x", *, address=TOKEN, log_index=0) -> RawLog:
    return RawLog(
        address=address,
        topics=tuple(topics),
        data=data,
        block_number=100,
        block_hash="0x" + "ee" * 32,
        tx_hash=TX,
        tx_index=0,
        log_index=log_index,
    )


def context(**overrides) -> TokenContext:
    base = {
        "address": TOKEN,
        "symbol": "TKN",
        "decimals": 0,
        "supply": 10_000,
        "wallets": {WALLET: "deployer"},
    }
    base.update(overrides)
    return TokenContext(**base)


def run(log: RawLog, ctx: TokenContext | None = None):
    return detect(
        log, ctx or context(), chain=Chain.ROBINHOOD, explorer_url=EXPLORER, thresholds=LIMITS
    )


def transfer(sender: str, receiver: str, amount: int, **kwargs) -> RawLog:
    return make_log([TRANSFER, pad(sender), pad(receiver)], "0x" + word(amount), **kwargs)


# --- mints -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "severity"),
    [(1, Severity.WARNING), (499, Severity.WARNING), (500, Severity.CRITICAL)],
)
def test_mint_severity_escalates_with_size(amount, severity):
    (detection,) = run(transfer(ZERO_ADDRESS, OTHER, amount))
    assert detection.event.rule is Rule.SUPPLY_MINTED
    assert detection.event.severity is severity
    assert detection.wallet is None


def test_mint_without_a_supply_snapshot_still_alerts_without_a_percentage():
    (detection,) = run(transfer(ZERO_ADDRESS, OTHER, 400), context(supply=None))
    assert detection.event.severity is Severity.WARNING
    assert "%" not in detection.event.explanation


@pytest.mark.parametrize(
    "log",
    [transfer(ZERO_ADDRESS, OTHER, 0), transfer(ZERO_ADDRESS, ZERO_ADDRESS, 500)],
)
def test_empty_or_pointless_mints_are_ignored(log):
    assert run(log) == []


# --- large transfers ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "severity"),
    [(99, None), (100, Severity.WARNING), (499, Severity.WARNING), (500, Severity.CRITICAL)],
)
def test_large_transfer_boundaries_are_inclusive(amount, severity):
    detections = run(transfer(WALLET, OTHER, amount))
    if severity is None:
        assert detections == []
        return
    (detection,) = detections
    assert detection.event.rule is Rule.LARGE_TRANSFER_OUT
    assert detection.event.severity is severity
    assert detection.wallet == WALLET
    assert "deployer wallet" in detection.event.explanation


@pytest.mark.parametrize("destination", [ZERO_ADDRESS, DEAD])
def test_sending_to_a_burn_address_is_info_and_says_so(destination):
    (detection,) = run(transfer(WALLET, destination, 1000))
    assert detection.event.severity is Severity.INFO
    assert "burn" in detection.event.explanation


def test_transfers_from_unwatched_wallets_are_ignored():
    assert run(transfer(OTHER, WALLET, 9999)) == []


def test_large_transfer_rule_is_off_without_a_supply_snapshot():
    assert run(transfer(WALLET, OTHER, 9999), context(supply=None)) == []


# --- malformed and look-alike logs -----------------------------------------------------------


def test_erc721_style_transfer_is_ignored():
    log = make_log([TRANSFER, pad(WALLET), pad(OTHER), "0x" + word(7)], "0x")
    assert len(log.topics) == 4
    assert run(log) == []


@pytest.mark.parametrize(
    "log",
    [
        make_log([TRANSFER, pad(WALLET), pad(OTHER)], "0x" + "00" * 31),  # short data
        make_log([TRANSFER, pad(WALLET), pad(OTHER)], "0x" + "00" * 64),  # long data
        make_log([TRANSFER, "0x" + "11" * 32, pad(OTHER)], "0x" + word(9999)),  # dirty padding
        make_log([TRANSFER, pad(WALLET)], "0x" + word(9999)),  # too few topics
        make_log(["0x" + "99" * 32, pad(WALLET), pad(OTHER)], "0x" + word(9999)),  # unknown event
        make_log([], "0x"),  # anonymous event
        transfer(WALLET, OTHER, 9999, address=OTHER),  # not the watched token
    ],
)
def test_logs_we_cannot_trust_or_do_not_know_are_ignored(log):
    assert run(log) == []


# --- ownership, upgrades, pausing -------------------------------------------------------------


def test_ownership_transfer_is_a_warning_and_reports_the_new_owner():
    log = make_log([OWNERSHIP_TRANSFERRED, pad(WALLET), pad(OTHER)])
    (detection,) = run(log)
    assert detection.event.rule is Rule.OWNERSHIP_TRANSFERRED
    assert detection.event.severity is Severity.WARNING
    assert detection.new_owner == OTHER


def test_renouncing_ownership_is_info():
    (detection,) = run(make_log([OWNERSHIP_TRANSFERRED, pad(WALLET), pad(ZERO_ADDRESS)]))
    assert detection.event.rule is Rule.OWNERSHIP_RENOUNCED
    assert detection.event.severity is Severity.INFO
    assert detection.new_owner == ZERO_ADDRESS


def test_ownership_event_with_the_wrong_shape_is_ignored():
    assert run(make_log([OWNERSHIP_TRANSFERRED, pad(WALLET)])) == []
    assert run(make_log([OWNERSHIP_TRANSFERRED, "0x" + "11" * 32, pad(OTHER)])) == []


def test_proxy_upgrade_is_critical():
    (detection,) = run(make_log([UPGRADED, pad(OTHER)]))
    assert detection.event.rule is Rule.PROXY_UPGRADED
    assert detection.event.severity is Severity.CRITICAL
    assert short_address(OTHER) in detection.event.explanation


def test_admin_change_decodes_both_addresses_from_data():
    data = "0x" + word(int(WALLET, 16)) + word(int(OTHER, 16))
    (detection,) = run(make_log([ADMIN_CHANGED], data))
    assert detection.event.rule is Rule.ADMIN_CHANGED
    assert detection.event.severity is Severity.WARNING
    assert short_address(WALLET) in detection.event.explanation
    assert short_address(OTHER) in detection.event.explanation


@pytest.mark.parametrize(
    "log",
    [
        make_log([PAUSED], "0x" + word(int(WALLET, 16))),  # account in data
        make_log([PAUSED, pad(WALLET)]),  # account indexed
    ],
)
def test_pause_is_critical_whichever_way_the_account_is_encoded(log):
    (detection,) = run(log)
    assert detection.event.rule is Rule.PAUSED
    assert detection.event.severity is Severity.CRITICAL
    assert short_address(WALLET) in detection.event.explanation


def test_pause_without_an_account_still_alerts():
    (detection,) = run(make_log([PAUSED]))
    assert detection.event.severity is Severity.CRITICAL
    assert " by " not in detection.event.explanation


def test_unpause_is_info_and_malformed_pause_logs_are_ignored():
    (detection,) = run(make_log([UNPAUSED]))
    assert detection.event.rule is Rule.UNPAUSED
    assert detection.event.severity is Severity.INFO
    assert run(make_log([PAUSED, pad(WALLET), pad(OTHER)])) == []


# --- event fields and hygiene -----------------------------------------------------------------


def test_event_carries_the_log_position_and_a_transaction_link():
    (detection,) = run(transfer(ZERO_ADDRESS, OTHER, 100, log_index=7))
    event = detection.event
    assert (event.tx_id, event.log_index, event.block_number) == (TX, 7, 100)
    assert event.explorer_url == f"{EXPLORER}/tx/{TX}"
    assert event.token_address == TOKEN


def test_hostile_symbol_cannot_inject_lines_or_invisible_text():
    ctx = context(symbol="EVIL\n\u202eX")
    (detection,) = run(transfer(ZERO_ADDRESS, OTHER, 100), ctx)
    assert "\n" not in detection.event.explanation
    assert "\u202e" not in detection.event.explanation


def test_amounts_are_formatted_with_decimals():
    assert format_amount(1_000_500_000_000_000_000_000, 18, "BASK") == "1,000.5 BASK"
    assert format_amount(5, 0, None) == "5"
    assert format_amount(7, None, "X") == "7 base units X"


# --- real data: the deployment transactions captured by scripts/capture_logs.py ------------------


def real_logs(name: str):
    saved = json.loads((FIXTURES / "logs" / f"{name}.json").read_text(encoding="utf-8"))
    return saved["token"], [parse_log(raw) for raw in saved["logs"]]


@pytest.mark.parametrize("name", ["nautcat", "basket"])
def test_real_logs_parse_and_belong_to_the_token(name):
    token, logs = real_logs(name)
    assert all(log.address == token for log in logs)


def test_real_mints_are_detected_and_prove_our_transfer_hash_matches_the_chain():
    mints = 0
    for name in ("nautcat", "basket"):
        token, logs = real_logs(name)
        ctx = TokenContext(address=token, decimals=18, supply=10**27)
        for log in logs:
            if log.topics and log.topics[0] == TRANSFER and log.topics[1] == pad(ZERO_ADDRESS):
                mints += 1
                detections = run(log, ctx)
                assert [d.event.rule for d in detections] == [Rule.SUPPLY_MINTED]
                assert detections[0].event.block_number == log.block_number
    assert mints > 0, "no real mint found: the computed Transfer topic does not match the chain"


def test_real_ownership_logs_decode_when_present():
    token, logs = real_logs("basket")
    ctx = TokenContext(address=token, decimals=18, supply=10**27)
    found = [log for log in logs if log.topics and log.topics[0] == OWNERSHIP_TRANSFERRED]
    if not found:
        pytest.skip("no real OwnershipTransferred log captured yet")
    for log in found:
        (detection,) = run(log, ctx)
        assert detection.event.rule in (Rule.OWNERSHIP_TRANSFERRED, Rule.OWNERSHIP_RENOUNCED)