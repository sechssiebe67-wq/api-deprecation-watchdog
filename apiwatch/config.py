"""Configuration loaded from environment variables.

Keeps all deployment knobs in one place so the rest of the code does not read
``os.environ`` directly.
"""
import os
from dataclasses import dataclass, field
from typing import List, Optional


def _bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    port: int = 8080
    host: str = "0.0.0.0"
    data_dir: str = "/data"
    poll_interval_seconds: int = 3600
    fetch_timeout_seconds: int = 20
    disable_scheduler: bool = False
    api_token: Optional[str] = None
    slack_webhook_url: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from: Optional[str] = None
    smtp_to: List[str] = field(default_factory=list)
    smtp_starttls: bool = True
    seed_sources: bool = True

    @property
    def db_path(self) -> str:
        return os.path.join(self.data_dir, "watchdog.db")

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "Config":
        env = os.environ if env is None else env
        recipients = [
            addr.strip()
            for addr in (env.get("SMTP_TO") or "").split(",")
            if addr.strip()
        ]
        return cls(
            port=int(env.get("PORT") or 8080),
            host=env.get("HOST") or "0.0.0.0",
            data_dir=env.get("DATA_DIR") or "/data",
            poll_interval_seconds=int(env.get("POLL_INTERVAL_SECONDS") or 3600),
            fetch_timeout_seconds=int(env.get("FETCH_TIMEOUT_SECONDS") or 20),
            disable_scheduler=_bool(env.get("DISABLE_SCHEDULER")),
            api_token=env.get("API_TOKEN") or None,
            slack_webhook_url=env.get("SLACK_WEBHOOK_URL") or None,
            smtp_host=env.get("SMTP_HOST") or None,
            smtp_port=int(env.get("SMTP_PORT") or 587),
            smtp_user=env.get("SMTP_USER") or None,
            smtp_password=env.get("SMTP_PASSWORD") or None,
            smtp_from=env.get("SMTP_FROM") or None,
            smtp_to=recipients,
            smtp_starttls=_bool(env.get("SMTP_STARTTLS"), default=True),
            seed_sources=_bool(env.get("SEED_SOURCES"), default=True),
        )
