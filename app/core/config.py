from decimal import Decimal
from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Farm Management Operational API"
    app_env: str = "local"
    app_timezone: str = "Asia/Jakarta"
    database_url: str = "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/farm_management_operational"
    internal_api_key: str = ""
    internal_api_previous_key: str = ""
    jwt_secret: str = ""
    jwt_issuer: str = "fm-platform-api"
    jwt_audience: str = "fm-frontend"
    jwt_ttl_hours: int = Field(default=8, ge=1, le=24)
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)
    log_level: str = "INFO"
    # finance-service internal API (customers for ABK egg outflows, delivery of approved egg sales).
    # FINANCE_INTERNAL_API_KEY is the X-FM-Internal-Key that finance-service accepts.
    finance_base_url: str = ""
    finance_internal_api_key: str = ""
    finance_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    # Lele biomass analytics: assumed average daily gain (grams per fish per day) used when a group
    # has no real ADG from samplings yet, and the per-fish weight at which a group is ready to sell.
    lele_default_adg_g: Decimal = Field(default=Decimal("1.3"), gt=0)
    lele_target_weight_g: Decimal = Field(default=Decimal("142"), gt=0)

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone)

    def validate_runtime_secrets(self) -> None:
        if not self.jwt_secret or len(self.jwt_secret) < 32:
            raise RuntimeError("JWT_SECRET must contain at least 32 characters")
        if not self.internal_api_key or len(self.internal_api_key) < 24:
            raise RuntimeError("INTERNAL_API_KEY must contain at least 24 characters")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
