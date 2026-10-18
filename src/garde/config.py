from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables or .env."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    telegram_bot_token: SecretStr  # required: startup fails loudly without it
    log_level: str = "INFO"
    max_tokens_per_chat: int = 5
    max_tokens_global: int = 100
    database_url: str = "sqlite+aiosqlite:///./garde.db"

    robinhood_chain_id: int = 4663
    robinhood_rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"
    robinhood_explorer_url: str = "https://robinhoodchain.blockscout.com"
    blockscout_api_url: str = "https://api.blockscout.com"
    blockscout_api_key: SecretStr | None = None
    helius_api_key: SecretStr | None = None

    holder_warning_pct: float = 20.0
    holder_critical_pct: float = 50.0

    scan_confirmations: int = Field(default=50, ge=0, le=10_000)
    scan_chunk_blocks: int = Field(default=10_000, ge=1, le=100_000)
    scan_max_backfill_blocks: int = Field(default=500_000, ge=1)
    scan_hash_window_blocks: int = Field(default=15_000, ge=100)
    scan_poll_seconds: float = Field(default=2.0, gt=0)
    scan_backoff_base_seconds: float = Field(default=1.0, gt=0)
    scan_backoff_max_seconds: float = Field(default=60.0, gt=0)

    @model_validator(mode="after")
    def _thresholds_make_sense(self) -> "Settings":
        if not 0 < self.holder_warning_pct < self.holder_critical_pct <= 100:
            raise ValueError("need 0 < HOLDER_WARNING_PCT < HOLDER_CRITICAL_PCT <= 100")
        return self

    @model_validator(mode="after")
    def _backoff_is_ordered(self) -> "Settings":
        if self.scan_backoff_max_seconds < self.scan_backoff_base_seconds:
            raise ValueError("SCAN_BACKOFF_MAX_SECONDS must be >= SCAN_BACKOFF_BASE_SECONDS")
        return self