import pytest
from pydantic import ValidationError

from garde.domain import Chain, CheckReport, Finding, RiskEvent, Rule, Severity, TokenInfo

TOKEN = "0x" + "ab" * 20


def make_event(**overrides) -> RiskEvent:
    base = dict(
        chain=Chain.ROBINHOOD,
        token_address=TOKEN,
        rule=Rule.SUPPLY_MINTED,
        severity=Severity.WARNING,
        tx_id="0x" + "cd" * 32,
        log_index=3,
        explanation="New supply was minted.",
        explorer_url="https://example.invalid/tx/1",
    )
    base.update(overrides)
    return RiskEvent(**base)


def test_dedup_key_format():
    event = make_event()
    assert event.dedup_key == f"robinhood:0x{'cd' * 32}:3:supply_minted"


def test_evm_tx_hash_case_does_not_create_a_second_event():
    lower = make_event(tx_id="0x" + "cd" * 32)
    upper = make_event(tx_id="0x" + "CD" * 32)
    assert lower.dedup_key == upper.dedup_key


def test_solana_signature_case_is_preserved():
    a = make_event(chain=Chain.SOLANA, tx_id="AbCdEf")
    b = make_event(chain=Chain.SOLANA, tx_id="abcdef")
    assert a.dedup_key != b.dedup_key


def test_negative_log_index_rejected():
    with pytest.raises(ValidationError):
        make_event(log_index=-1)


def test_event_is_immutable():
    event = make_event()
    with pytest.raises(ValidationError):
        event.log_index = 99


def test_report_without_liquidity_finding_is_rejected():
    token = TokenInfo(chain=Chain.ROBINHOOD, address=TOKEN)
    with pytest.raises(ValidationError):
        CheckReport(token=token, findings=[Finding(key="proxy", summary="Not a proxy.")])


def test_report_with_invisible_liquidity_is_valid():
    token = TokenInfo(chain=Chain.ROBINHOOD, address=TOKEN)
    finding = Finding(key="liquidity", summary="Liquidity not visible.", visible=False)
    assert CheckReport(token=token, findings=[finding]).findings[0].visible is False