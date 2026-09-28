from functools import lru_cache
from typing import Literal, Optional
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    app_env: str = 'development'
    trading_mode: Literal['paper', 'demo-orders'] = 'paper'
    execution_enabled: Literal[False] = False
    allow_live_trading: Literal[False] = False
    demo_execution_enabled: bool = False
    demo_ctrader_account_id: Optional[str] = None
    signal_max_age_seconds: int = Field(default=90, ge=5, le=600)
    demo_preflight_ttl_seconds: int = Field(default=86400, ge=60, le=604800)
    esses_broker_execution: bool = False
    paper_scheduler_enabled: bool = False
    paper_account_id: Optional[str] = None
    # Optional explicit research-only waiver, never represents verified news.
    paper_allow_unknown_news: bool = False
    postgres_host: str = 'postgres'
    postgres_db: str = 'mrmburu'
    postgres_user: str = 'mrmburu'
    postgres_password: SecretStr
    redis_url: str = 'redis://redis:6379/0'
    admin_api_key: SecretStr
    research_api_key: SecretStr
    deepseek_api_key: Optional[SecretStr] = None
    fred_api_key: Optional[SecretStr] = None
    ctrader_client_id: Optional[SecretStr] = None
    ctrader_client_secret: Optional[SecretStr] = None
    ctrader_access_token: Optional[SecretStr] = None
    ctrader_account_id: Optional[str] = None
    ctrader_network_enabled: bool = False
    ctrader_environment: Literal['demo', 'live'] = 'demo'
    paper_strategy: Literal['sma', 'esses-v1'] = 'esses-v1'
    ctrader_cached_feed: bool = True
    ctrader_requests_per_minute: int = Field(default=60, ge=1, le=100)
    ctrader_requests_per_24h: int = Field(default=1000, ge=1, le=1000)
    ctrader_redirect_uri: str = 'https://trader.acshop.shop/research/ctrader/callback'
    ctrader_broker_orders: bool = False
    prop_sim_ctrader_account_id: Optional[str] = None
    prop_sim_execution_enabled: Literal[False] = False
    prop_sim_allowed_account_ids: str = '17204978'
    prop_sim_acknowledged_live_environment: bool = False
    discord_bot_enabled: bool = False
    discord_bot_token: Optional[SecretStr] = None
    discord_api_key: Optional[SecretStr] = None
    discord_guild_id: Optional[int] = None
    discord_admin_role_id: Optional[int] = None
    discord_channel_id: Optional[int] = None
    discord_allowed_user_ids: str = ''
    discord_api_url: str = 'http://api:8000'
    discord_rate_limit_per_minute: int = Field(default=10, ge=1, le=60)

    @field_validator('execution_enabled', 'allow_live_trading', 'prop_sim_execution_enabled', mode='before')
    @classmethod
    def coerce_execution_enabled(cls, value):
        # Env vars arrive as strings; Literal[False] rejects "false" without coercion.
        if isinstance(value, str) and value.strip().lower() in {'false', '0', 'no', 'off', ''}:
            return False
        return value

    @field_validator('deepseek_api_key', 'fred_api_key', 'ctrader_client_id', 'ctrader_client_secret', 'ctrader_access_token', 'discord_bot_token', 'discord_api_key', mode='before')
    @classmethod
    def empty_secret_is_none(cls, value):
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @model_validator(mode='after')
    def validate_keys(self):
        keys = [self.admin_api_key.get_secret_value(), self.research_api_key.get_secret_value()]
        if any(len(k) < 32 for k in keys) or keys[0] == keys[1]:
            raise ValueError('Use distinct admin and research keys of at least 32 characters')
        if len(self.postgres_password.get_secret_value()) < 24:
            raise ValueError('Generate a database password of at least 24 characters')
        if self.discord_api_key:
            discord_key = self.discord_api_key.get_secret_value()
            if len(discord_key) < 32 or discord_key in keys:
                raise ValueError('DISCORD_API_KEY must be distinct and at least 32 characters')
        if self.discord_bot_enabled:
            if not all((self.discord_bot_token, self.discord_api_key, self.discord_guild_id,
                        self.discord_admin_role_id, self.discord_channel_id,
                        self.discord_allowed_user_ids.strip())):
                raise ValueError('Discord bot configuration is incomplete')
        from services.demo_orders.guards import validate_settings
        from services.ctrader.guards_accounts import FORBIDDEN_EXECUTION_ACCOUNTS
        validate_settings(self)
        prop_id = (self.prop_sim_ctrader_account_id or '').strip()
        if prop_id in FORBIDDEN_EXECUTION_ACCOUNTS:
            raise ValueError('48803059 cannot be used as PROP_SIM_CTRADER_ACCOUNT_ID')
        allowed = [item.strip() for item in (self.prop_sim_allowed_account_ids or '').split(',') if item.strip()]
        if any(item in FORBIDDEN_EXECUTION_ACCOUNTS for item in allowed):
            raise ValueError('48803059 cannot be allowlisted for prop-sim')
        if prop_id and allowed and prop_id not in allowed:
            raise ValueError('PROP_SIM_CTRADER_ACCOUNT_ID must be in PROP_SIM_ALLOWED_ACCOUNT_IDS')
        return self

    @property
    def database_url(self):
        return URL.create('postgresql+psycopg', username=self.postgres_user,
                          password=self.postgres_password.get_secret_value(),
                          host=self.postgres_host, database=self.postgres_db)

@lru_cache
def get_settings():
    return Settings()
