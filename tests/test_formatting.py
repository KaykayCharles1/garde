from garde.bot.formatting import DISCLAIMER, TELEGRAM_LIMIT, format_report
from garde.domain import Chain, CheckReport, Finding, Severity, TokenInfo

ADDRESS = "0x" + "ab" * 20


def report(*, name="Test", blind_spots=("one thing",)):
    token = TokenInfo(chain=Chain.ROBINHOOD, address=ADDRESS, name=name, symbol="TST", decimals=18)
    findings = [
        Finding(key="source_verified", summary="Source verified."),
        Finding(key="liquidity", severity=Severity.WARNING, visible=False, summary="Not seen."),
    ]
    return CheckReport(token=token, findings=findings, blind_spots=list(blind_spots))


def test_report_contains_link_disclaimer_and_visibility_labels():
    text = format_report(report(), "https://explorer.invalid/address/x")
    assert "https://explorer.invalid/address/x" in text
    assert DISCLAIMER in text
    assert "[NOT VISIBLE]" in text
    assert "[INFO]" in text
    assert "What Garde cannot see:" in text


def test_oversized_report_is_truncated_but_keeps_the_disclaimer():
    text = format_report(report(blind_spots=["long " * 100] * 50), "https://x.invalid")
    assert len(text) <= TELEGRAM_LIMIT
    assert text.endswith(DISCLAIMER)


def test_hostile_token_name_cannot_break_the_header():
    text = format_report(report(name="Evil\nFake line\u202e"), "https://x.invalid")
    assert text.splitlines()[0].startswith("Check: TST")
    assert "\u202e" not in text