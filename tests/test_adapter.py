import pytest

from garde.chains.base import ChainAdapter
from garde.chains.fake import FakeAdapter
from garde.domain import WatchTarget
from tests.test_domain import TOKEN, make_event


def test_interface_cannot_be_instantiated():
    with pytest.raises(TypeError):
        ChainAdapter()


async def test_fake_check_always_reports_liquidity():
    report = await FakeAdapter().check_token(TOKEN.upper().replace("0X", "0x"))
    assert any(f.key == "liquidity" for f in report.findings)


async def test_poll_only_returns_events_for_watched_tokens():
    other = "0x" + "11" * 20
    adapter = FakeAdapter(events=[make_event(), make_event(token_address=other, log_index=9)])
    events = await adapter.poll_events([WatchTarget(chain=adapter.chain, token_address=TOKEN)])
    assert [e.token_address for e in events] == [TOKEN]
    assert adapter.poll_calls == 1


async def test_fake_snapshot_returns_what_the_tests_configure():
    adapter = FakeAdapter()
    adapter.owner = "0x" + "22" * 20
    snapshot = await adapter.snapshot_for_watch(TOKEN)
    assert snapshot.owner == "0x" + "22" * 20
    assert snapshot.token.symbol == "FAKE"
    assert len(snapshot.top_wallets) == 3