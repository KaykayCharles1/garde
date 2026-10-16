"""Capture real API responses as test fixtures and print their shape.

Run from the repo root:  python scripts/capture_fixtures.py
URLs are never printed, because they contain the Blockscout API key.
"""

import json
import sys
from pathlib import Path

import httpx

from garde.config import Settings

TOKENS = {
    "nautcat": "0xe4D5B1f1A64099c7C38D003AD93196258aE57A03",
    "basket": "0x9e00366E55E10E71e4576aBAFeFe3C847Cfb6754",
    "unverified": "0x5Ae4ac3deE7F774869D116e85b2305535560328B",
}

BLOCKSCOUT_PATHS = {
    "address": "addresses/{a}",
    "contract": "smart-contracts/{a}",
    "token": "tokens/{a}",
    "holders": "tokens/{a}/holders",
}

OWNER_SELECTOR = "0x8da5cb5b"  # owner()
IMPL_SLOT = "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc"
ADMIN_SLOT = "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103"
FIXTURE_DIR = Path("tests/fixtures")


def rpc_calls(address: str) -> dict[str, dict]:
    def call(method: str, params: list) -> dict:
        return {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}

    return {
        "rpc_owner": call("eth_call", [{"to": address, "data": OWNER_SELECTOR}, "latest"]),
        "rpc_impl_slot": call("eth_getStorageAt", [address, IMPL_SLOT, "latest"]),
        "rpc_admin_slot": call("eth_getStorageAt", [address, ADMIN_SLOT, "latest"]),
    }


def shape(value, values: bool = True, depth: int = 0, max_depth: int = 4):
    """Describe the structure of JSON: field names, types, and (optionally) a short sample."""
    if isinstance(value, dict):
        if depth >= max_depth:
            return f"dict({len(value)} keys)"
        return {k: shape(v, values, depth + 1, max_depth) for k, v in value.items()}
    if isinstance(value, list):
        if not value:
            return "list(empty)"
        if depth >= max_depth:
            return f"list({len(value)})"
        return {f"list({len(value)}) first item": shape(value[0], values, depth + 1, max_depth)}
    name = type(value).__name__
    return f"{name}: {repr(value)[:30]}" if values else name


def show(name: str, label: str, status: int, body, seen: dict) -> None:
    dumped = json.dumps(body)
    print(f"\n== {name} / {label} / HTTP {status}")
    if len(dumped) < 300:
        print(dumped)
        return
    structure = shape(body, values=False)
    if seen.get(label) == structure:
        print(f"(same structure as an earlier token's {label})")
        return
    seen[label] = structure
    print(json.dumps(shape(body), indent=2))


def record(name: str, label: str, response: httpx.Response, key: str, seen: dict) -> None:
    try:
        body = response.json()
    except ValueError:
        body = response.text
    body = json.loads(json.dumps(body).replace(key, "REDACTED"))
    folder = FIXTURE_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    saved = {"status": response.status_code, "body": body}
    (folder / f"{label}.json").write_text(json.dumps(saved, indent=2), encoding="utf-8")
    show(name, label, response.status_code, body, seen)


def main() -> None:
    settings = Settings()
    secret = settings.blockscout_api_key
    key = secret.get_secret_value() if secret else ""
    if not key:
        sys.exit("BLOCKSCOUT_API_KEY is missing from .env")
    base = f"https://api.blockscout.com/{settings.robinhood_chain_id}/api/v2"
    seen: dict = {}
    try:
        with httpx.Client(timeout=20) as client:
            for name, address in TOKENS.items():
                for label, path in BLOCKSCOUT_PATHS.items():
                    url = f"{base}/{path.format(a=address)}"
                    response = client.get(url, params={"apikey": key})
                    record(name, label, response, key, seen)
                for label, payload in rpc_calls(address).items():
                    response = client.post(settings.robinhood_rpc_url, json=payload)
                    record(name, label, response, key, seen)
    except httpx.HTTPError as exc:
        # Only the exception type is printed: some messages contain the full URL.
        sys.exit(f"request failed: {type(exc).__name__}")


if __name__ == "__main__":
    main()