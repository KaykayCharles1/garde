import asyncio
from typing import Any, Protocol

import httpx

from garde.chains.errors import UpstreamError


class ExplorerApi(Protocol):
    """What the adapter needs from an explorer. Tests substitute a fake."""

    async def get_address(self, address: str) -> dict[str, Any] | None: ...

    async def get_holders(self, address: str) -> dict[str, Any] | None: ...


class BlockscoutClient:
    """Thin client for the two Blockscout v2 endpoints we verified: addresses and token holders."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        base_url: str,
        chain_id: int,
        api_key: str,
        *,
        retries: int = 2,
        backoff_base: float = 0.5,
    ) -> None:
        self._http = http
        self._root = f"{base_url.rstrip('/')}/{chain_id}/api/v2"
        self._api_key = api_key
        self._retries = retries
        self._backoff_base = backoff_base

    async def get_address(self, address: str) -> dict[str, Any] | None:
        return await self._get(f"addresses/{address}")

    async def get_holders(self, address: str) -> dict[str, Any] | None:
        return await self._get(f"tokens/{address}/holders")

    async def _get(self, path: str) -> dict[str, Any] | None:
        """Returns the JSON object, or None on 404. Raises UpstreamError for everything else.

        Error messages never include the URL, because it contains the API key.
        """
        url = f"{self._root}/{path}"
        problem = "unknown"
        for attempt in range(self._retries + 1):
            try:
                response = await self._http.get(url, params={"apikey": self._api_key})
            except httpx.TransportError as exc:  # timeouts, connection failures
                problem = type(exc).__name__
            else:
                status = response.status_code
                if status == 404:
                    return None
                if status == 200:
                    return self._parse(response)
                if status != 429 and status < 500:
                    raise UpstreamError(f"explorer returned HTTP {status}")
                problem = f"HTTP {status}"
            if attempt < self._retries:
                await asyncio.sleep(self._backoff_base * 2**attempt)
        raise UpstreamError(f"explorer unavailable ({problem})")

    @staticmethod
    def _parse(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            raise UpstreamError("explorer returned invalid JSON") from None
        if not isinstance(data, dict):
            raise UpstreamError("explorer returned unexpected JSON")
        return data