from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    database_url: str = "sqlite:///./demo.db"
    redis_url: str = "redis://localhost:6379/0"
    demo_mode: bool = True
    http_timeout: float = 10.0
    discovery_lease_seconds: int = Field(default=120, ge=10, le=3600)
    live_public_fetch_enabled: bool = False
    fetch_max_bytes: int = Field(default=262144, ge=1024, le=1048576)
    fetch_max_redirects: int = Field(default=3, ge=0, le=5)
    fetch_concurrency: int = Field(default=2, ge=1, le=8)
    fetch_domain_interval: float = Field(default=1, ge=0.1, le=10)
    fetch_total_timeout: float = Field(default=15, ge=1, le=30)


@lru_cache
def get_settings() -> Settings:
    return Settings()
