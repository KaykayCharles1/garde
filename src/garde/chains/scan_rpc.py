"""Plain JSON-RPC access for the block scanner.

Only calls verified with scripts/probe_rpc.py are used: eth_blockNumber,
eth_getBlockByNumber (headers only), eth_getLogs, and batch requests.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from garde.chains.errors import RpcRateLimited, RpcRejected, UpstreamError
from garde.sanitize import clean_text

_HASH_RE = re.compile(r"0x[0-9a-fA-F]{64}")
_ADDRESS_RE = re.compile(r"0x[0-9a-fA-F]{40}")
_HEX_RE = re.compile(r"0x[0-9a-fA-F]+")
_DATA_RE = re.compile(r"0x[0-9a-fA-F]*")


@dataclass(frozen=True)
class BlockHeader:
    number: int
    hash: str
    parent_hash: str
    timestamp: int


@dataclass(frozen=True)
class RawLog:
    address: str
    topics: tuple[str, ...]
    data: str
    block_number: int
    block_hash: str
    tx_hash: str
    tx_index: int
    log_index: int


class ScanRpc(Protocol):
    """What the scanner needs from a node. Tests substitute a simulated chain."""

    async def block_number(self) -> int: ...

    async def get_headers(self, heights: Sequence[int]) -> list[BlockHeader | None]:
        """One entry per requested height, in order. None means the node has no such block."""

    async def get_logs(
        self, from_block: int, to_block: int, log_filter: dict[str, Any]
    ) -> list[RawLog]: ...


def _hex_int(value: object) -> int:
    if not isinstance(value, str) or not _HEX_RE.fullmatch(value):
        raise UpstreamError("rpc returned a malformed number")
    return int(value, 16)


def _hash(value: object) -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        raise UpstreamError("rpc returned a malformed hash")
    return value.lower()


def parse_header(raw: object) -> BlockHeader:
    if not isinstance(raw, dict):
        raise UpstreamError("rpc returned a malformed block")
    return BlockHeader(
        number=_hex_int(raw.get("number")),
        hash=_hash(raw.get("hash")),
        parent_hash=_hash(raw.get("parentHash")),
        timestamp=_hex_int(raw.get("timestamp")),
    )


def parse_log(raw: object) -> RawLog:
    if not isinstance(raw, dict):
        raise UpstreamError("rpc returned a malformed log")
    address = raw.get("address")
    topics = raw.get("topics")
    data = raw.get("data")
    if not isinstance(address, str) or not _ADDRESS_RE.fullmatch(address):
        raise UpstreamError("rpc returned a malformed log address")
    if not isinstance(topics, list) or not isinstance(data, str) or not _DATA_RE.fullmatch(data):
        raise UpstreamError("rpc returned a malformed log")
    return RawLog(
        address=address.lower(),
        topics=tuple(_hash(topic) for topic in topics),
        data=data,
        block_number=_hex_int(raw.get("blockNumber")),
        block_hash=_hash(raw.get("blockHash")),
        tx_hash=_hash(raw.get("transactionHash")),
        tx_index=_hex_int(raw.get("transactionIndex")),
        log_index=_hex_int(raw.get("logIndex")),
    )


def _call(method: str, params: list, request_id: int) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


class HttpScanRpc:
    """JSON-RPC over httpx. Error messages never include the URL."""

    def __init__(self, http: httpx.AsyncClient, url: str, *, batch_limit: int = 50) -> None:
        self._http = http
        self._url = url
        self._batch_limit = batch_limit

    async def block_number(self) -> int:
        body = await self._post(_call("eth_blockNumber", [], 1))
        return _hex_int(self._unwrap(body))

    async def get_headers(self, heights: Sequence[int]) -> list[BlockHeader | None]:
        unique = list(dict.fromkeys(heights))
        found: dict[int, BlockHeader | None] = {}
        for start in range(0, len(unique), self._batch_limit):
            found.update(await self._header_batch(unique[start : start + self._batch_limit]))
        return [found[height] for height in heights]

    async def get_logs(
        self, from_block: int, to_block: int, log_filter: dict[str, Any]
    ) -> list[RawLog]:
        params = {**log_filter, "fromBlock": hex(from_block), "toBlock": hex(to_block)}
        result = self._unwrap(await self._post(_call("eth_getLogs", [params], 1)))
        if not isinstance(result, list):
            raise UpstreamError("rpc returned unexpected logs")
        logs = []
        for raw in result:
            if isinstance(raw, dict) and raw.get("removed") is True:
                continue
            logs.append(parse_log(raw))
        return logs

    async def _header_batch(self, heights: list[int]) -> dict[int, BlockHeader | None]:
        payload = [
            _call("eth_getBlockByNumber", [hex(height), False], index)
            for index, height in enumerate(heights)
        ]
        try:
            items = self._batch_items(await self._post(payload), len(payload))
        except RpcRejected:
            if len(heights) == 1:
                raise
            middle = len(heights) // 2  # the node refused the batch: try smaller ones
            left = await self._header_batch(heights[:middle])
            right = await self._header_batch(heights[middle:])
            return {**left, **right}
        found: dict[int, BlockHeader | None] = {}
        for item, height in zip(items, heights, strict=True):
            result = self._unwrap(item)
            if result is None:
                found[height] = None
                continue
            header = parse_header(result)
            if header.number != height:
                raise UpstreamError("rpc returned the wrong block")
            found[height] = header
        return found


    @staticmethod
    def _label(payload: Any) -> str:
        first = payload[0] if isinstance(payload, list) and payload else payload
        method = first.get("method") if isinstance(first, dict) else None
        return clean_text(method, 40) if method else "request"

    async def _post(self, payload: Any) -> Any:
        label = self._label(payload)
        try:
            response = await self._http.post(self._url, json=payload)
        except httpx.TransportError as exc:
            raise UpstreamError(f"rpc {label} unreachable ({type(exc).__name__})") from None
        if response.status_code == 429:
            raise RpcRateLimited(f"rpc {label} HTTP 429")
        if response.status_code >= 500:
            raise UpstreamError(f"rpc {label} HTTP {response.status_code}")
        try:
            return response.json()
        except ValueError:
            raise UpstreamError(
                f"rpc {label} HTTP {response.status_code} without valid JSON"
            ) from None

    @staticmethod
    def _unwrap(item: Any) -> Any:
        """The result of one JSON-RPC response object. Error objects become RpcRejected."""
        if not isinstance(item, dict):
            raise UpstreamError("rpc returned an unexpected response")
        if "error" in item:
            error = item["error"]
            message = error.get("message") if isinstance(error, dict) else error
            raise RpcRejected(clean_text(message, 160))
        if "result" not in item:
            raise UpstreamError("rpc response had no result")
        return item["result"]

    @staticmethod
    def _batch_items(body: Any, expected: int) -> list[Any]:
        if isinstance(body, dict):
            HttpScanRpc._unwrap(body)  # one error object for the whole batch raises RpcRejected
            raise UpstreamError("rpc answered a batch with a single result")
        if not isinstance(body, list):
            raise UpstreamError("rpc returned an unexpected batch response")
        by_id = {item.get("id"): item for item in body if isinstance(item, dict)}
        try:
            return [by_id[index] for index in range(expected)]
        except KeyError:
            raise UpstreamError("rpc batch response is missing items") from None