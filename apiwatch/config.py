"""Configuration loaded from environment variables.

Keeps all deployment knobs in one place so the rest of the code does not read
``os.environ`` directly.
"""
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional

from .models import ALL_CHANNELS, NOTIFY_CHANNELS

log = logging.getLogger("apiwatch.config")

DIGEST_MODES = ("off", "digest", "both")
DIGEST_INTERVAL_MIN_SECONDS = 300


def _bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _digest_mode(value: Optional[str]) -> str:
    mode = (value or "off").strip().lower()
    if mode not in DIGEST_MODES:
        log.warning("invalid DIGEST_MODE %r; falling back to 'off'", value)
        return "off"
    return mode


def _notify_channels(value: Optional[str]) -> List[str]:
    """Parse ``NOTIFY_CHANNELS`` into the default routing for new sources."""
    channels = [
        item.strip().lower() for item in (value or "").split(",") if item.strip()
    ]
    if not channels:
        return [ALL_CHANNELS]
    allowed = set(NOTIFY_CHANNELS) | {ALL_CHANNELS}
    invalid = [channel for channel in channels if channel not in allowed]
    if invalid:
        log.warning("invalid NOTIFY_CHANNELS %r; falling back to 'all'", value)
        return [ALL_CHANNELS]
    return channels


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
    webhook_url: Optional[str] = None
    smtp_host: Optional[str] = None
    smtp_port: int = 587
    smtp_user: Optional[str] = None
    smtp_password: Optional[str] = None
    smtp_from: Optional[str] = None
    smtp_to: List[str] = field(default_factory=list)
    smtp_starttls: bool = True
    seed_sources: bool = True
    notify_channels: List[str] = field(default_factory=lambda: [ALL_CHANNELS])
    digest_mode: str = "off"
    digest_interval_seconds: int = 86400
    digest_max_alerts: int = 50

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
            webhook_url=env.get("WEBHOOK_URL") or None,
            smtp_host=env.get("SMTP_HOST") or None,
            smtp_port=int(env.get("SMTP_PORT") or 587),
            smtp_user=env.get("SMTP_USER") or None,
            smtp_password=env.get("SMTP_PASSWORD") or None,
            smtp_from=env.get("SMTP_FROM") or None,
            smtp_to=recipients,
            smtp_starttls=_bool(env.get("SMTP_STARTTLS"), default=True),
            seed_sources=_bool(env.get("SEED_SOURCES"), default=True),
            notify_channels=_notify_channels(env.get("NOTIFY_CHANNELS")),
            digest_mode=_digest_mode(env.get("DIGEST_MODE")),
            digest_interval_seconds=max(
                DIGEST_INTERVAL_MIN_SECONDS,
                int(env.get("DIGEST_INTERVAL_SECONDS") or 86400),
            ),
            digest_max_alerts=max(1, int(env.get("DIGEST_MAX_ALERTS") or 50)),
        )
