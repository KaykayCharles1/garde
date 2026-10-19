"""Probe how the RPC handles eth_getLogs for a busy contract. Read-only.

Run from the repo root:  python scripts/probe_busy.py [address]
Defaults to the BASKET fixture's top holder (a PoolManager contract).
The RPC URL is not printed.
"""

import json
import sys
import time
from pathlib import Path

import httpx

from garde.config import Settings

RANGES = (100, 1_000, 5_000, 10_000)
BURST_CALLS = 12
BURST_RANGE = 1_000


def default_address() -> str:
    saved = json.loads(Path("tests/fixtures/basket/holders.json").read_text(encoding="utf-8"))
    return saved["body"]["items"][0]["address"]["hash"].lower()


def get_logs(client: httpx.Client, url: str, address: str, start: int, end: int):
    params = [{"address": [address], "fromBlock": hex(start), "toBlock": hex(end)}]
    payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_getLogs", "params": params}
    began = time.monotonic()
    try:
        response = client.post(url, json=payload)
    except httpx.HTTPError as exc:
        return None, f"failed: {type(exc).__name__}", time.monotonic() - began
    took = time.monotonic() - began
    try:
        body = response.json()
    except ValueError:
        body = None
    count, note = "?", ""
    if isinstance(body, dict):
        if isinstance(body.get("result"), list):
            count = str(len(body["result"]))
        elif "error" in body:
            error = body["error"]
            note = " error=" + str(error.get("message") if isinstance(error, dict) else error)[:100]
    retry = response.headers.get("retry-after")
    summary = f"HTTP {response.status_code}, {len(response.content)} bytes, logs={count}"
    if retry:
        summary += f", retry-after={retry}"
    return response.status_code, summary + note, took


def main() -> None:
    address = sys.argv[1].lower() if len(sys.argv) > 1 else default_address()
    url = Settings().robinhood_rpc_url
    with httpx.Client(timeout=60) as client:
        try:
            head_payload = {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}
            head = int(client.post(url, json=head_payload).json()["result"], 16)
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            sys.exit(f"could not read the head block: {type(exc).__name__}")
        print(f"address under test: {address[:10]}...  head: {head}")
        for size in RANGES:
            _, summary, took = get_logs(client, url, address, head - size, head)
            print(f"{size:>6} blocks: {summary} ({took:.1f}s)")
        statuses: dict = {}
        for i in range(BURST_CALLS):
            end = head - 20_000 - i * BURST_RANGE
            status, _, _ = get_logs(client, url, address, end - BURST_RANGE, end)
            statuses[status] = statuses.get(status, 0) + 1
        print(f"burst of {BURST_CALLS} back-to-back {BURST_RANGE}-block calls: statuses {statuses}")


if __name__ == "__main__":
    main()