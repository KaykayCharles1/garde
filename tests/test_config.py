import pytest
from pydantic import ValidationError

from garde.config import Settings


def test_missing_token_fails(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)  # _env_file=None ignores your real .env during tests


def test_defaults_and_secret_masking(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fake-token")
    s = Settings(_env_file=None)
    assert s.max_tokens_per_chat == 5
    assert s.max_tokens_global == 100
    assert s.robinhood_chain_id == 4663
    assert s.telegram_bot_token.get_secret_value() == "123:fake-token"
    assert "fake-token" not in str(s.telegram_bot_token)  # masked when printed


@pytest.mark.parametrize("warn,critical", [(60, 50), (0, 50), (20, 101), (50, 50)])
def test_nonsense_holder_thresholds_are_rejected(warn, critical):
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            telegram_bot_token="123:fake-token",
            holder_warning_pct=warn,
            holder_critical_pct=critical,
        )    