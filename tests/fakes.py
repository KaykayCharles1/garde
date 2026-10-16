import copy
import json
from pathlib import Path
from typing import Any

from garde.chains.errors import CallReverted
from garde.chains.robinhood import IMPLEMENTATION_SLOT, RobinhoodAdapter

FIXTURES = Path(__file__).parent / "fixtures"
ZERO_WORD = bytes(32)


def load_fixture(name: str, label: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name / f"{label}.json").read_text(encoding="utf-8"))


class FakeExplorer:
    """Stands in for Blockscout. Tests mutate .address / .holders or set an error to raise."""

    def __init__(self, address: dict | None, holders: dict | None) -> None:
        self.address = address
        self.holders = holders
        self.address_error: Exception | None = None
        self.holders_error: Exception | None = None

    async def get_address(self, address: str) -> dict | None:
        if self.address_error:
            raise self.address_error
        return self.address

    async def get_holders(self, address: str) -> dict | None:
        if self.holders_error:
            raise self.holders_error
        return self.holders


def fixture_explorer(name: str) -> FakeExplorer:
    def body(label: str) -> dict | None:
        saved = load_fixture(name, label)
        return None if saved["status"] == 404 else copy.deepcopy(saved["body"])

    return FakeExplorer(address=body("address"), holders=body("holders"))


class ScriptedRpc:
    """Each attribute is bytes to return, or an Exception to raise."""

    def __init__(self, owner=b"", impl=ZERO_WORD, admin=ZERO_WORD) -> None:
        self.owner = owner
        self.impl = impl
        self.admin = admin

    async def call_owner(self, address: str) -> bytes:
        return self._resolve(self.owner)

    async def read_storage(self, address: str, slot: int) -> bytes:
        return self._resolve(self.impl if slot == IMPLEMENTATION_SLOT else self.admin)

    @staticmethod
    def _resolve(value):
        if isinstance(value, Exception):
            raise value
        return value


def fixture_rpc(name: str) -> ScriptedRpc:
    """Replays the real RPC responses captured in tests/fixtures."""

    def decode(label: str):
        body = load_fixture(name, label)["body"]
        if "error" in body:
            return CallReverted("execution reverted")
        return bytes.fromhex(body["result"].removeprefix("0x"))

    return ScriptedRpc(
        owner=decode("rpc_owner"),
        impl=decode("rpc_impl_slot"),
        admin=decode("rpc_admin_slot"),
    )


def make_adapter(explorer: FakeExplorer, rpc: ScriptedRpc) -> RobinhoodAdapter:
    return RobinhoodAdapter(
        explorer,
        rpc,
        explorer_url="https://explorer.invalid",
        warn_pct=20.0,
        critical_pct=50.0,
    )