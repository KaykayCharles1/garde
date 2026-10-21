from garde.bot.formatting import DISCLAIMER
from garde.delivery import messages

FIELDS = {
    "severity": "critical",
    "symbol": "TKN",
    "chain": "robinhood",
    "token_address": "0x" + "aa" * 20,
    "explanation": "The token contract was paused.",
    "explorer_url": "https://explorer.invalid/tx/1",
    "block_number": 100,
}


def test_alert_has_severity_name_explanation_link_and_disclaimer():
    text = messages.render_alert(**FIELDS)
    assert "CRITICAL" in text
    assert "TKN" in text
    assert "The token contract was paused." in text
    assert "https://explorer.invalid/tx/1" in text
    assert "Block 100" in text
    assert text.endswith(DISCLAIMER)


def test_retraction_and_reconfirmation_say_what_happened():
    retraction = messages.render_retraction(**FIELDS)
    assert "retracted" in retraction.lower()
    assert "reorganization" in retraction
    assert "The token contract was paused." in retraction
    reconfirmed = messages.render_reconfirmed(**FIELDS)
    assert "back on chain" in reconfirmed


def test_gap_notice_counts_and_names_the_range():
    text = messages.render_gap(chain="robinhood", count=2, first=100, last=500)
    assert "2 stretches" in text
    assert "blocks 100 to 500" in text
    assert "1 stretch " in messages.render_gap(chain="robinhood", count=1, first=1, last=2)


def test_hostile_and_unusual_data_cannot_break_the_message():
    text = messages.render_alert(
        **{**FIELDS, "symbol": "EVIL\n\u202eX", "severity": "nonsense", "explanation": "a\nb" * 800}
    )
    assert "\u202e" not in text
    assert "INFO" in text  # unknown severity falls back to info
    assert len(text) < 4096


def test_missing_block_and_symbol_are_handled():
    text = messages.render_alert(**{**FIELDS, "symbol": None, "block_number": None})
    assert "0xaaaa" in text
    assert "Block" not in text