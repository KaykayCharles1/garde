"""Probe eth_getLogs filter limits on the Robinhood Chain RPC. Read-only.

Run from the repo root:  python scripts/probe_filters.py
"""

import sys
import time

import httpx
from web3 import Web3

from garde.config import Settings

NAUTCAT = "0xe4d5b1f1a64099c7c38d003ad93196258ae57a03"
TRANSFER = "0x" + bytes(Web3.keccak(text="Transfer(address,address,uint256)")).hex()
SIZES = (1, 10, 50, 100, 500, 1000)


def fake_address(index: int) -> str:
    return "0x" + f"{index + 1:040x}"


def padded_topic(index: int) -> str:
    return "0x" + "00" * 12 + f"{index + 1:040x}"


def call(client: httpx.Client, url: str, method: str, params: list):
    try:
        body = client.post(
            url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).json()
    except (httpx.HTTPError, ValueError) as exc:
        return None, type(exc).__name__
    if not isinstance(body, dict):
        return None, "unexpected response"
    if "error" in body:
        error = body["error"]
        return None, str(error.get("message") if isinstance(error, dict) else error)[:160]
    return body.get("result"), None


def main() -> None:
    url = Settings().robinhood_rpc_url
    with httpx.Client(timeout=30) as client:
        head_hex, error = call(client, url, "eth_blockNumber", [])
        if error:
            sys.exit(f"eth_blockNumber failed: {error}")
        head = int(head_hex, 16)

        def probe(label: str, log_filter: dict) -> None:
            started = time.monotonic()
            result, error = call(client, url, "eth_getLogs", [log_filter])
            took = time.monotonic() - started
            count = len(result) if isinstance(result, list) else "?"
            outcome = f"error: {error}" if error else f"ok, {count} logs"
            print(f"{label}: {outcome} ({took:.1f}s)")

        window = {"fromBlock": hex(head - 100), "toBlock": hex(head)}
        for size in SIZES:
            probe(f"{size:>5} addresses", {**window, "address": [fake_address(i) for i in range(size)]})
        for size in SIZES:
            topics = [[TRANSFER], [padded_topic(i) for i in range(size)]]
            probe(f"{size:>5} 'from' topics, no address filter", {**window, "topics": topics})
        probe(
            "NAUTCAT + Transfer topic, 10k blocks",
            {"fromBlock": hex(head - 10_000), "toBlock": hex(head), "address": [NAUTCAT],
             "topics": [[TRANSFER]]},
        )


if __name__ == "__main__":
    main()