"""Runtime configuration, read from environment variables (and an optional .env file)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPO_ROOT = Path(__file__).resolve().parents[2]


def redact_url(url: str) -> str:
    """``opc.tcp://user:secret@host:4840`` -> ``opc.tcp://***@host:4840`` (for logs and /api/status)."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<invalid url>"
    if "@" not in parts.netloc:
        return url
    return urlunsplit(parts._replace(netloc="***@" + parts.netloc.rsplit("@", 1)[1]))


class Settings(BaseSettings):
    # the repository's .env (docker compose uses the same file) and one in the working directory
    model_config = SettingsConfigDict(env_file=(REPO_ROOT / ".env", ".env"), env_file_encoding="utf-8", extra="ignore")

    # --- general -----------------------------------------------------------
    log_level: str = "INFO"
    log_format: Literal["json", "text"] = "json"
    timezone: str = "Europe/Berlin"
    # Shown in the header badge: "live" for the plant, "demo"/"replay" for the mock server.
    source_label: Literal["live", "demo", "replay"] = "live"

    # --- configuration files ------------------------------------------------
    plant_config_path: Path = REPO_ROOT / "config" / "plant.yaml"
    nodes_config_path: Path = REPO_ROOT / "config" / "opcua_nodes.yaml"

    # --- database -------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://dmf:dmf@localhost:5432/dmf"
    db_run_migrations: bool = True
    alembic_ini_path: Path = REPO_ROOT / "database" / "alembic.ini"
    db_pool_size: int = Field(5, ge=1, le=50)
    event_queue_max: int = Field(100_000, ge=100)
    # events kept in memory while the database is unreachable (oldest dropped beyond this;
    # must hold more than one write batch of 500)
    db_buffer_max: int = Field(200_000, ge=1000)
    db_retry_max_s: float = Field(30.0, gt=0)

    # --- OPC UA (read-only) ---------------------------------------------------
    opcua_endpoint: str = "opc.tcp://192.168.0.10:4840"
    opcua_username: str | None = None
    opcua_password: str | None = None
    # e.g. "Basic256Sha256,SignAndEncrypt,/certs/client.der,/certs/client.pem"; empty = no security
    opcua_security: str | None = None
    opcua_timeout_s: float = Field(4.0, gt=0)
    opcua_publishing_interval_ms: int = Field(100, ge=10)
    opcua_sampling_interval_ms: int = Field(50, ge=0)
    opcua_queue_size: int = Field(10, ge=1)
    opcua_subscribe_sensors: bool = True
    opcua_reconnect_min_s: float = Field(1.0, gt=0)
    opcua_reconnect_max_s: float = Field(30.0, gt=0)
    opcua_validate_nodes: bool = True
    opcua_enabled: bool = True

    # --- tracking ------------------------------------------------------------
    handover_window_s: float = Field(8.0, gt=0)
    stuck_after_s: float = Field(120.0, gt=0)
    reconcile_max_hops: int = Field(6, ge=1)
    tick_interval_s: float = Field(1.0, gt=0)
    snapshot_interval_s: float = Field(10.0, gt=0)

    # --- offline detection -----------------------------------------------------
    # off        : offline only when the server is unreachable or values are Bad
    # timestamps : additionally read each cell's lamp SourceTimestamp periodically
    offline_detection: Literal["off", "timestamps"] = "off"
    offline_after_s: float = Field(60.0, gt=0)
    offline_check_interval_s: float = Field(10.0, ge=1)

    # --- API ---------------------------------------------------------------------
    cors_origins: str = ""
    ws_max_rate_hz: float = Field(4.0, gt=0, le=50)
    ws_max_clients: int = Field(50, ge=1)
    export_max_days: int = Field(31, ge=1, le=366)
    history_page_max: int = Field(200, ge=1, le=10_000)

    @field_validator("opcua_username", "opcua_password", "opcua_security", mode="before")
    @classmethod
    def _empty_is_none(cls, v: str | None) -> str | None:
        return v or None

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {v!r} (use an IANA name such as Europe/Berlin)") from exc
        return v

    @field_validator("database_url")
    @classmethod
    def _asyncpg_url(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must start with postgresql+asyncpg://")
        return v

    @field_validator("opcua_endpoint")
    @classmethod
    def _opc_url(cls, v: str) -> str:
        if not v.startswith("opc.tcp://"):
            raise ValueError("OPCUA_ENDPOINT must start with opc.tcp://")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> Settings:
        if self.opcua_reconnect_max_s < self.opcua_reconnect_min_s:
            raise ValueError("OPCUA_RECONNECT_MAX_S must be >= OPCUA_RECONNECT_MIN_S")
        if self.offline_detection == "timestamps" and self.offline_after_s <= self.offline_check_interval_s:
            raise ValueError("OFFLINE_AFTER_S must be larger than OFFLINE_CHECK_INTERVAL_S")
        return self

    @property
    def opcua_endpoint_redacted(self) -> str:
        return redact_url(self.opcua_endpoint)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
