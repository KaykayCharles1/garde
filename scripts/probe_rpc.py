"""Probe the Robinhood Chain RPC for the facts the M3 scanner depends on.

Run from the repo root:  python scripts/probe_rpc.py
Read-only. The RPC URL is not printed.
"""

import sys
import time

import httpx

from garde.config import Settings

NAUTCAT = "0xe4d5b1f1a64099c7c38d003ad93196258ae57a03"
BASKET = "0x9e00366e55e10e71e4576abafefe3c847cfb6754"
LOG_RANGES = (10, 100, 1_000, 10_000, 100_000)


def request(method: str, params: list, request_id: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def call(client: httpx.Client, url: str, method: str, params: list):
    """Returns (value, error). error is None on success; the value itself may be None."""
    try:
        response = client.post(url, json=request(method, params))
        body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        return None, type(exc).__name__
    if not isinstance(body, dict):
        return None, f"unexpected response (HTTP {response.status_code})"
    if "error" in body:
        error = body["error"]
        message = error.get("message") if isinstance(error, dict) else error
        return None, str(message)[:160]
    if "result" not in body:
        return None, f"no result (HTTP {response.status_code})"
    return body["result"], None


def main() -> None:
    url = Settings().robinhood_rpc_url
    with httpx.Client(timeout=30) as client:
        head_hex, error = call(client, url, "eth_blockNumber", [])
        if error:
            sys.exit(f"eth_blockNumber failed: {error}")
        head = int(head_hex, 16)
        print(f"head block: {head}")

        # 1. Do blocks carry the fields reorg detection needs?
        newest, error = call(client, url, "eth_getBlockByNumber", [hex(head), False])
        if error or not isinstance(newest, dict):
            sys.exit(f"eth_getBlockByNumber failed: {error or 'null block'}")
        missing = [k for k in ("hash", "parentHash", "number", "timestamp") if k not in newest]
        print(f"block fields missing: {missing or 'none'}")

        # 2. How fast are blocks?
        older, error = call(client, url, "eth_getBlockByNumber", [hex(head - 200), False])
        if isinstance(older, dict):
            seconds = int(newest["timestamp"], 16) - int(older["timestamp"], 16)
            print(f"average block time over 200 blocks: {seconds / 200:.3f}s")
        else:
            print(f"could not fetch an older block: {error}")

        # 3. What do the 'safe' and 'finalized' tags mean on this chain?
        for tag in ("safe", "finalized"):
            tagged, error = call(client, url, "eth_getBlockByNumber", [tag, False])
            if error:
                print(f"tag {tag}: error: {error}")
            elif isinstance(tagged, dict):
                behind = head - int(tagged["number"], 16)
                print(f"tag {tag}: {behind} blocks behind head")
            else:
                print(f"tag {tag}: returned null")

        # 4. Does the endpoint accept batch requests?
        try:
            batch = [request("eth_blockNumber", [], 1), request("eth_blockNumber", [], 2)]
            response = client.post(url, json=batch)
            body = response.json()
            ok = isinstance(body, list) and len(body) == 2
            print(f"batch requests: {'supported' if ok else 'NOT supported'} "
                  f"(HTTP {response.status_code})")
        except (httpx.HTTPError, ValueError) as exc:
            print(f"batch requests: failed ({type(exc).__name__})")

        # 5. How large a getLogs range does the endpoint allow?
        for size in LOG_RANGES:
            params = [
                {"fromBlock": hex(head - size), "toBlock": hex(head), "address": [NAUTCAT, BASKET]}
            ]
            started = time.monotonic()
            logs, error = call(client, url, "eth_getLogs", params)
            took = time.monotonic() - started
            count = len(logs) if isinstance(logs, list) else "?"
            outcome = f"error: {error}" if error else f"ok, {count} logs"
            print(f"getLogs {size:>7} blocks: {outcome} ({took:.1f}s)")

        # 6. Does a burst of calls get rate limited?
        statuses: dict[int, int] = {}
        started = time.monotonic()
        try:
            for _ in range(30):
                response = client.post(url, json=request("eth_blockNumber", []))
                statuses[response.status_code] = statuses.get(response.status_code, 0) + 1
        except httpx.HTTPError as exc:
            print(f"burst test stopped early: {type(exc).__name__}")
        print(f"burst: {sum(statuses.values())} calls in {time.monotonic() - started:.1f}s, "
              f"HTTP statuses {statuses}")


if __name__ == "__main__":
    main()