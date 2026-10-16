import httpx
import pytest

from garde.chains.blockscout import BlockscoutClient
from garde.chains.errors import UpstreamError

KEY = "SECRET-KEY-123"


async def fetch(handler, *, retries: int = 2):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = BlockscoutClient(
            http, "https://explorer.invalid", 4663, KEY, retries=retries, backoff_base=0.0
        )
        return await client.get_address("0xabc")


async def test_success_returns_json_and_sends_key_and_path():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"hash": "0xabc"})

    assert await fetch(handler) == {"hash": "0xabc"}
    assert seen[0].url.path == "/4663/api/v2/addresses/0xabc"
    assert seen[0].url.params["apikey"] == KEY


async def test_404_returns_none_without_retrying():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404, json={"message": "Not found"})

    assert await fetch(handler) is None
    assert len(calls) == 1


async def test_429_is_retried_then_succeeds():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429)
        return httpx.Response(200, json={"ok": True})

    assert await fetch(handler) == {"ok": True}
    assert len(calls) == 2


async def test_persistent_server_error_raises_after_all_retries():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500)

    with pytest.raises(UpstreamError):
        await fetch(handler, retries=2)
    assert len(calls) == 3


async def test_bad_key_fails_fast_without_retry():
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(401)

    with pytest.raises(UpstreamError, match="401"):
        await fetch(handler)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, content=b"<html>not json</html>"),
        httpx.Response(200, json=["a", "list"]),
    ],
)
async def test_malformed_success_body_raises(response):
    with pytest.raises(UpstreamError):
        await fetch(lambda request: response)


async def test_network_failure_raises_without_leaking_url_or_key():
    def handler(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(UpstreamError) as info:
        await fetch(handler)
    assert KEY not in str(info.value)
    assert "explorer.invalid" not in str(info.value)