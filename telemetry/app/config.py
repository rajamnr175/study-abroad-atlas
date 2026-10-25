"""Settings, read once from the environment."""
import os
from dataclasses import dataclass, field


def _list(name: str, default: str) -> list[str]:
    return [s.strip().rstrip("/") for s in os.getenv(name, default).split(",") if s.strip()]


@dataclass(frozen=True)
class Settings:
    # sqlite:///./telemetry.db for local runs; postgresql+psycopg://user:pass@host/db in production
    database_url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///./telemetry.db"))
    # Pages allowed to send events. Requests from any other Origin are refused.
    allowed_origins: list[str] = field(default_factory=lambda: _list(
        "ALLOWED_ORIGINS",
        "https://rajamnr175.github.io,http://localhost:8000,http://127.0.0.1:8000",
    ))
    # Bearer token for GET /v1/stats. Unset means the stats endpoint is off.
    admin_token: str = field(default_factory=lambda: os.getenv("ADMIN_TOKEN", ""))
    # Batches per minute from one IP address (the address is used in memory only, never stored).
    rate_limit_per_min: int = field(default_factory=lambda: int(os.getenv("RATE_LIMIT_PER_MIN", "120")))
    # Behind a load balancer (Render, Fly, Railway...) the client IP is in X-Forwarded-For.
    trust_proxy: bool = field(default_factory=lambda: os.getenv("TRUST_PROXY", "false").lower() == "true")
    max_body_bytes: int = 64 * 1024
    max_events_per_batch: int = 100


def get_settings() -> Settings:
    return Settings()
