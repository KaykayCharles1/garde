import hashlib
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any

from garde.chains.errors import RpcRejected
from garde.chains.scan_rpc import BlockHeader, RawLog

ZERO_HASH = "0x" + "00" * 32


def make_hash(*parts: object) -> str:
    return "0x" + hashlib.sha256(":".join(str(p) for p in parts).encode()).hexdigest()


class SimChain:
    """A fake node. Hashes commit to the parent hash, so a reorg changes every later hash."""

    def __init__(self) -> None:
        self.epoch = 0
        self.blocks: list[dict[str, Any]] = [
            {"hash": make_hash("genesis"), "parent": ZERO_HASH, "logs": []}
        ]
        self.max_span: int | None = None  # reject getLogs ranges longer than this
        self.head_override: int | None = None  # simulate a node that is behind
        self.hidden: set[int] = set()  # heights the node pretends not to have
        self.fail_on: dict[str, list[Exception]] = {}  # method name -> errors to raise, in order
        self.on_get_logs: Callable[[], None] | None = None  # runs once, inside getLogs
        self.injected: list[RawLog] = []  # extra (bad) logs returned by getLogs
        self.calls: Counter[str] = Counter()

    @property
    def head(self) -> int:
        return len(self.blocks) - 1

    def hash_at(self, height: int) -> str:
        return self.blocks[height]["hash"]

    def mine(self, count: int = 1) -> None:
        for _ in range(count):
            parent = self.blocks[-1]
            number = len(self.blocks)
            self.blocks.append(
                {
                    "hash": make_hash(self.epoch, number, parent["hash"]),
                    "parent": parent["hash"],
                    "logs": [],
                }
            )

    def add_log(
        self, height: int, address: str, *, log_index: int = 0, tx_hash: str | None = None
    ) -> RawLog:
        log = RawLog(
            address=address,
            topics=(make_hash("topic"),),
            data="0x",
            block_number=height,
            block_hash=self.hash_at(height),
            tx_hash=tx_hash or make_hash("tx", self.epoch, height, log_index),
            tx_index=0,
            log_index=log_index,
        )
        self.blocks[height]["logs"].append(log)
        return log

    def reorg(self, fork_height: int, new_blocks: int) -> None:
        """Keep blocks up to fork_height, replace everything after with new_blocks new ones."""
        del self.blocks[fork_height + 1 :]
        self.epoch += 1
        self.mine(new_blocks)

    # ---- the ScanRpc interface --------------------------------------------------------

    def _begin(self, name: str) -> None:
        self.calls[name] += 1
        queue = self.fail_on.get(name)
        if queue:
            raise queue.pop(0)

    async def block_number(self) -> int:
        self._begin("block_number")
        return self.head if self.head_override is None else self.head_override

    async def get_headers(self, heights: Sequence[int]) -> list[BlockHeader | None]:
        self._begin("get_headers")
        out: list[BlockHeader | None] = []
        for height in heights:
            if height in self.hidden or not 0 <= height <= self.head:
                out.append(None)
                continue
            block = self.blocks[height]
            out.append(BlockHeader(height, block["hash"], block["parent"], height))
        return out

    async def get_logs(
        self, from_block: int, to_block: int, log_filter: dict[str, Any]
    ) -> list[RawLog]:
        self._begin("get_logs")
        if self.on_get_logs is not None:
            hook, self.on_get_logs = self.on_get_logs, None
            hook()
        if self.max_span is not None and to_block - from_block + 1 > self.max_span:
            raise RpcRejected("query spans too many blocks")
        wanted = {a.lower() for a in log_filter.get("address", [])}
        logs = [
            log
            for height in range(from_block, min(to_block, self.head) + 1)
            for log in self.blocks[height]["logs"]
            if not wanted or log.address in wanted
        ]
        return logs + self.injected