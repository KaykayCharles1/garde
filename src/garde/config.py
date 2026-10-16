from pydantic import SecretStr, model_validator
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

    @model_validator(mode="after")
    def _thresholds_make_sense(self) -> "Settings":
        if not 0 < self.holder_warning_pct < self.holder_critical_pct <= 100:
            raise ValueError("need 0 < HOLDER_WARNING_PCT < HOLDER_CRITICAL_PCT <= 100")
        return self