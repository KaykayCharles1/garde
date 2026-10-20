import json

import httpx
import pytest

from garde.chains.errors import RpcRateLimited, RpcRejected, UpstreamError
from garde.chains.scan_rpc import HttpScanRpc

URL = "https://rpc.invalid/secret-path"


def header_json(number: int) -> dict:
    return {
        "number": hex(number),
        "hash": "0x" + "a" * 64,
        "parentHash": "0x" + "c" * 64,
        "timestamp": hex(number),
    }


def log_json(**overrides) -> dict:
    base = {
        "address": "0x" + "AA" * 20,
        "topics": ["0x" + "BB" * 32],
        "data": "0x",
        "blockNumber": "0x64",
        "blockHash": "0x" + "cc" * 32,
        "transactionHash": "0x" + "dd" * 32,
        "transactionIndex": "0x1",
        "logIndex": "0x2",
        "removed": False,
    }
    return {**base, **overrides}


async def run(handler, action, **kwargs):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        return await action(HttpScanRpc(http, URL, **kwargs))


def answer_headers(items):
    return [
        {"jsonrpc": "2.0", "id": item["id"], "result": header_json(int(item["params"][0], 16))}
        for item in items
    ]


async def test_block_number_parses_hex():
    def handler(request):
        body = json.loads(request.content)
        assert body["method"] == "eth_blockNumber"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": "0x10"})

    assert await run(handler, lambda rpc: rpc.block_number()) == 16


async def test_headers_come_back_in_order_even_if_the_server_shuffles_and_one_is_missing():
    def handler(request):
        items = json.loads(request.content)
        assert all(item["params"][1] is False for item in items)
        out = [
            {"jsonrpc": "2.0", "id": item["id"], "result": None}
            if item["params"][0] == "0x7"
            else answer_headers([item])[0]
            for item in items
        ]
        out.reverse()  # servers may answer a batch in any order
        return httpx.Response(200, json=out)

    headers = await run(handler, lambda rpc: rpc.get_headers([5, 7, 6]))
    assert [h.number if h else None for h in headers] == [5, None, 6]


async def test_big_requests_are_split_into_batches():
    sizes = []

    def handler(request):
        items = json.loads(request.content)
        sizes.append(len(items))
        return httpx.Response(200, json=answer_headers(items))

    await run(handler, lambda rpc: rpc.get_headers([1, 2, 3, 4, 5]), batch_limit=2)
    assert sizes == [2, 2, 1]


async def test_a_batch_the_node_refuses_is_retried_in_halves():
    sizes = []

    def handler(request):
        items = json.loads(request.content)
        sizes.append(len(items))
        if len(items) > 2:
            error = {"code": -32600, "message": "batch too large"}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": None, "error": error})
        return httpx.Response(200, json=answer_headers(items))

    headers = await run(handler, lambda rpc: rpc.get_headers([1, 2, 3, 4, 5]))
    assert [h.number for h in headers] == [1, 2, 3, 4, 5]
    assert sizes == [5, 2, 3, 1, 2]


async def test_wrong_block_in_the_answer_is_rejected():
    def handler(request):
        items = json.loads(request.content)
        return httpx.Response(
            200,
            json=[{"jsonrpc": "2.0", "id": items[0]["id"], "result": header_json(99)}],
        )

    with pytest.raises(UpstreamError, match="wrong block"):
        await run(handler, lambda rpc: rpc.get_headers([5]))


@pytest.mark.parametrize("status", [200, 400])
async def test_json_rpc_errors_become_rpc_rejected_whatever_the_http_status(status):
    def handler(request):
        error = {"code": -32000, "message": "query spans 100001 blocks, only 100000 allowed"}
        return httpx.Response(status, json={"jsonrpc": "2.0", "id": 1, "error": error})

    with pytest.raises(RpcRejected, match="query spans"):
        await run(handler, lambda rpc: rpc.get_logs(1, 100_001, {"address": []}))


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_server_trouble_is_an_upstream_error_not_a_rejection(status):
    with pytest.raises(UpstreamError) as info:
        await run(lambda request: httpx.Response(status), lambda rpc: rpc.block_number())
    assert not isinstance(info.value, RpcRejected)


async def test_network_failure_does_not_leak_the_url():
    def handler(request):
        raise httpx.ConnectError("boom")

    with pytest.raises(UpstreamError) as info:
        await run(handler, lambda rpc: rpc.block_number())
    assert "rpc.invalid" not in str(info.value)
    assert "secret-path" not in str(info.value)


async def test_logs_are_parsed_normalized_and_removed_ones_dropped():
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        result = [log_json(), log_json(removed=True, logIndex="0x3")]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    address_filter = {"address": ["0x" + "aa" * 20]}
    logs = await run(handler, lambda rpc: rpc.get_logs(100, 200, address_filter))
    assert len(logs) == 1
    log = logs[0]
    assert log.address == "0x" + "aa" * 20
    assert log.topics == ("0x" + "bb" * 32,)
    assert (log.block_number, log.log_index, log.tx_index) == (100, 2, 1)
    params = seen[0]["params"][0]
    assert params["fromBlock"] == "0x64"
    assert params["toBlock"] == "0xc8"
    assert params["address"] == ["0x" + "aa" * 20]


@pytest.mark.parametrize(
    "bad",
    [
        "not a dict",
        log_json(blockHash=None),
        log_json(logIndex="0xzz"),
        log_json(address="0x1234"),
        log_json(topics="nope"),
        log_json(data=5),
    ],
)
async def test_malformed_logs_are_rejected(bad):
    def handler(request):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": [bad]})

    with pytest.raises(UpstreamError):
        await run(handler, lambda rpc: rpc.get_logs(1, 2, {}))


async def test_logs_result_must_be_a_list():
    def handler(request):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"oops": 1}})

    with pytest.raises(UpstreamError):
        await run(handler, lambda rpc: rpc.get_logs(1, 2, {}))


async def test_server_trouble_names_the_failing_method():
    with pytest.raises(UpstreamError, match="eth_getLogs"):
        await run(lambda request: httpx.Response(429), lambda rpc: rpc.get_logs(1, 2, {}))


async def test_http_429_is_reported_as_rate_limiting():
    with pytest.raises(RpcRateLimited, match="eth_getLogs"):
        await run(lambda request: httpx.Response(429), lambda rpc: rpc.get_logs(1, 2, {}))