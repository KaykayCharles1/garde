from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All configuration comes from environment variables or .env."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    telegram_bot_token: SecretStr  # required: no default, so startup fails loudly without it
    log_level: str = "INFO"
    max_tokens_per_chat: int = 5
    max_tokens_global: int = 100
    robinhood_chain_id: int = 4663
    robinhood_rpc_url: str = "https://rpc.mainnet.chain.robinhood.com"
    helius_api_key: SecretStr | None = None
    database_url: str = "sqlite+aiosqlite:///./garde.db"