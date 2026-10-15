from enum import StrEnum

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)


class Chain(StrEnum):
    ROBINHOOD = "robinhood"
    SOLANA = "solana"

    @property
    def is_evm(self) -> bool:
        return self is Chain.ROBINHOOD


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class Rule(StrEnum):
    """Stable rule ids. They are stored in the database, so never rename one."""

    OWNERSHIP_TRANSFERRED = "ownership_transferred"
    OWNERSHIP_RENOUNCED = "ownership_renounced"
    PROXY_UPGRADED = "proxy_upgraded"
    ADMIN_CHANGED = "admin_changed"
    PAUSED = "paused"
    UNPAUSED = "unpaused"
    SUPPLY_MINTED = "supply_minted"
    LARGE_TRANSFER_OUT = "large_transfer_out"
    LIQUIDITY_REMOVED = "liquidity_removed"
    MINT_AUTHORITY_CHANGED = "mint_authority_changed"
    FREEZE_AUTHORITY_CHANGED = "freeze_authority_changed"
    METADATA_CHANGED = "metadata_changed"


class RiskEvent(BaseModel):
    """One detected risk signal. Same shape for every chain."""

    model_config = ConfigDict(frozen=True)

    chain: Chain
    token_address: str
    rule: Rule
    severity: Severity
    tx_id: str  # EVM: tx hash. Solana: signature.
    log_index: int = Field(ge=0)  # EVM: log index. Solana: instruction index (scheme set in M4).
    explanation: str = Field(min_length=1)
    explorer_url: str = Field(min_length=1)
    block_number: int | None = None

    @field_validator("tx_id")
    @classmethod
    def _normalize_tx_id(cls, value: str, info: ValidationInfo) -> str:
        # EVM hashes are case-insensitive hex, so 0xAB and 0xab must be the same event.
        # Solana signatures are case-sensitive base58 and must not be touched.
        chain = info.data.get("chain")
        return value.lower() if chain is not None and chain.is_evm else value

    @property
    def dedup_key(self) -> str:
        # ":" separates the parts because rule ids contain "_", which would be ambiguous.
        return f"{self.chain.value}:{self.tx_id}:{self.log_index}:{self.rule.value}"


class TokenInfo(BaseModel):
    chain: Chain
    address: str
    name: str | None = None
    symbol: str | None = None
    decimals: int | None = Field(default=None, ge=0, le=255)
    total_supply: int | None = Field(default=None, ge=0)


class Finding(BaseModel):
    key: str  # e.g. "source_verified", "liquidity"
    summary: str = Field(min_length=1)  # plain language
    severity: Severity = Severity.INFO
    visible: bool = True  # False means "we could not see this"


class CheckReport(BaseModel):
    token: TokenInfo
    findings: list[Finding]
    blind_spots: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _must_report_liquidity(self) -> "CheckReport":
        # Encodes the rule "never stay silent about liquidity" in the type itself.
        if not any(f.key == "liquidity" for f in self.findings):
            raise ValueError("a CheckReport must include a 'liquidity' finding (visible or not)")
        return self


class WalletRef(BaseModel):
    address: str
    label: str | None = None  # e.g. "deployer", "treasury"


class WatchTarget(BaseModel):
    """What a scanner needs: one token plus every extra wallet any chat declared for it."""

    chain: Chain
    token_address: str
    wallets: list[WalletRef] = Field(default_factory=list)