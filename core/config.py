from functools import lru_cache
from typing import Literal
from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file='.env', extra='ignore')
    app_env: str = 'development'
    trading_mode: Literal['paper'] = 'paper'
    execution_enabled: Literal[False] = False
    postgres_host: str = 'postgres'
    postgres_db: str = 'mrmburu'
    postgres_user: str = 'mrmburu'
    postgres_password: SecretStr
    redis_url: str = 'redis://redis:6379/0'
    admin_api_key: SecretStr
    research_api_key: SecretStr

    @field_validator('execution_enabled', mode='before')
    @classmethod
    def coerce_execution_enabled(cls, value):
        # Env vars arrive as strings; Literal[False] rejects "false" without coercion.
        if isinstance(value, str) and value.strip().lower() in {'false', '0', 'no', 'off', ''}:
            return False
        return value

    @model_validator(mode='after')
    def validate_keys(self):
        keys = [self.admin_api_key.get_secret_value(), self.research_api_key.get_secret_value()]
        if any(len(k) < 32 for k in keys) or keys[0] == keys[1]:
            raise ValueError('Use distinct admin and research keys of at least 32 characters')
        if len(self.postgres_password.get_secret_value()) < 24:
            raise ValueError('Generate a database password of at least 24 characters')
        return self

    @property
    def database_url(self):
        return URL.create('postgresql+psycopg', username=self.postgres_user,
                          password=self.postgres_password.get_secret_value(),
                          host=self.postgres_host, database=self.postgres_db)

@lru_cache
def get_settings():
    return Settings()
