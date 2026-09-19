"""
Central config. Everything here is overridable via environment
variables or a .env file (see .env.example) so the four people
building this can each point at their own Etherscan key without
touching code.
"""
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    etherscan_api_key: str = ""
    chain_id: int = 1

    # Optional dedicated RPC endpoint (Ankr/Alchemy/etc). If blank,
    # RpcClient falls back to Etherscan's own proxy module, so the
    # app works out of the box with just an Etherscan key.
    rpc_url: str = ""

    etherscan_max_req_per_sec: float = 3.0

    new_counterparty_sample_size: int = 20

    # Etherscan free tier dropped its per-request row cap to 1,000
    # on 2026-07-01. Pagination logic in the client relies on this
    # number rather than the old 10,000 default.
    etherscan_page_size: int = 1000

    cache_ttl_seconds: int = 600


settings = Settings()
