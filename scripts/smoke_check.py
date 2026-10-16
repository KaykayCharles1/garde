"""Run the real adapter against the three fixture tokens. Run from the repo root.

    python scripts/smoke_check.py
"""

import asyncio
import sys

import httpx

from garde.bot.formatting import format_report
from garde.chains.blockscout import BlockscoutClient
from garde.chains.errors import NotATokenError, UpstreamError
from garde.chains.evm_rpc import Web3Rpc
from garde.chains.robinhood import RobinhoodAdapter
from garde.config import Settings

# Lowercased on purpose: this script tests the adapter, not your copy-paste casing.
TOKENS = {
    "nautcat": "0xe4D5B1f1A64099c7C38D003AD93196258aE57A03".lower(),
    "basket": "0x9e00366E55E10E71e4576aBAFeFe3C847Cfb6754".lower(),
    "wallet": "0x5Ae4ac3deE7F774869D116e85b2305535560328B".lower(),
}


async def main() -> None:
    settings = Settings()
    secret = settings.blockscout_api_key
    key = secret.get_secret_value() if secret else ""
    if not key:
        sys.exit("BLOCKSCOUT_API_KEY is missing from .env")
    async with httpx.AsyncClient(timeout=15) as http:
        adapter = RobinhoodAdapter(
            BlockscoutClient(http, settings.blockscout_api_url, settings.robinhood_chain_id, key),
            Web3Rpc(settings.robinhood_rpc_url),
            explorer_url=settings.robinhood_explorer_url,
            warn_pct=settings.holder_warning_pct,
            critical_pct=settings.holder_critical_pct,
        )
        for name, address in TOKENS.items():
            print(f"\n######## {name}\n")
            try:
                report = await adapter.check_token(address)
            except (NotATokenError, UpstreamError) as exc:
                print(f"{type(exc).__name__}: {exc}")
                continue
            print(format_report(report, adapter.explorer_address_url(address)))


if __name__ == "__main__":
    asyncio.run(main())