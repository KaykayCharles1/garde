from tests.fakes import FIXTURES


def test_fixtures_exist_and_contain_no_api_key_parameter():
    files = list(FIXTURES.rglob("*.json"))
    assert files, "no fixtures found: run python scripts/capture_fixtures.py"
    for path in files:
        assert "apikey" not in path.read_text(encoding="utf-8").lower(), path