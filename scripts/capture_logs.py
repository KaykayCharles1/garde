"""Save the deployment-transaction logs of the fixture tokens as real log fixtures.

Run from the repo root:  python scripts/capture_logs.py
Needs tests/fixtures/<token>/address.json from capture_fixtures.py. Read-only.
"""

import json
from collections import Counter
from pathlib import Path

import httpx

from garde.config import Settings

FIXTURES = Path("tests/fixtures")
TOKENS = {
    "nautcat": "0xe4d5b1f1a64099c7c38d003ad93196258ae57a03",
    "basket": "0x9e00366e55e10e71e4576abafefe3c847cfb6754",
}


def main() -> None:
    url = Settings().robinhood_rpc_url
    out_dir = FIXTURES / "logs"
    out_dir.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=30) as client:
        for name, token in TOKENS.items():
            saved = json.loads((FIXTURES / name / "address.json").read_text(encoding="utf-8"))
            tx_hash = saved["body"].get("creation_transaction_hash")
            if not tx_hash:
                print(f"{name}: no creation transaction in the address fixture")
                continue
            payload = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "eth_getTransactionReceipt",
                "params": [tx_hash],
            }
            try:
                body = client.post(url, json=payload).json()
            except (httpx.HTTPError, ValueError) as exc:
                print(f"{name}: request failed ({type(exc).__name__})")
                continue
            receipt = body.get("result") if isinstance(body, dict) else None
            if not isinstance(receipt, dict):
                print(f"{name}: no receipt returned: {str(body)[:200]}")
                continue
            logs = [
                log
                for log in receipt.get("logs", [])
                if str(log.get("address", "")).lower() == token
            ]
            output = {"token": token, "tx": tx_hash, "logs": logs}
            (out_dir / f"{name}.json").write_text(json.dumps(output, indent=2), encoding="utf-8")
            topics = Counter(
                (log["topics"][0][:12] if log.get("topics") else "none") for log in logs
            )
            print(f"{name}: {len(logs)} logs from the token in its deployment tx; topic0 {dict(topics)}")


if __name__ == "__main__":
    main()