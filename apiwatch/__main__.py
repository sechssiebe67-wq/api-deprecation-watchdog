"""Application entry point.

Wires configuration, storage, notifiers, engine and HTTP server together and
starts the background polling scheduler.
"""
import logging
import os

from .app import create_server
from .config import Config
from .engine import Engine, seed_default_sources
from .fetcher import Fetcher
from .notify import ConsoleNotifier, EmailNotifier, SlackNotifier, WebhookNotifier
from .scheduler import Scheduler
from .sources import default_sources
from .store import Store


def configure_logging() -> None:
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def build_notifiers(config: Config):
    notifiers = [ConsoleNotifier()]
    if config.slack_webhook_url:
        notifiers.append(SlackNotifier(config.slack_webhook_url))
    if config.webhook_url:
        notifiers.append(WebhookNotifier(config.webhook_url))
    if config.smtp_host and config.smtp_to:
        notifiers.append(
            EmailNotifier(
                host=config.smtp_host,
                port=config.smtp_port,
                username=config.smtp_user,
                password=config.smtp_password,
                sender=config.smtp_from,
                recipients=config.smtp_to,
                starttls=config.smtp_starttls,
            )
        )
    return notifiers


def build_app(config: Config = None):
    config = config or Config.from_env()
    store = Store(config.db_path)
    if config.seed_sources:
        seed_default_sources(store, default_sources())
    fetcher = Fetcher(timeout=config.fetch_timeout_seconds)
    notifiers: list = build_notifiers(config)
    engine = Engine(
        store,
        fetcher,
        notifiers,
        digest_mode=config.digest_mode,
        digest_max_alerts=config.digest_max_alerts,
        breaker_threshold=config.breaker_threshold,
        breaker_cooldown_seconds=config.breaker_cooldown_seconds,
    )
    server = create_server(store, engine, config)
    return config, store, engine, server


def main() -> None:
    configure_logging()
    config, store, engine, server = build_app()
    scheduler = None
    if not config.disable_scheduler:
        digest_interval = (
            config.digest_interval_seconds
            if config.digest_mode in ("digest", "both")
            else None
        )
        scheduler = Scheduler(
            engine,
            interval_seconds=config.poll_interval_seconds,
            digest_interval_seconds=digest_interval,
        )
        scheduler.start()
    logging.getLogger("apiwatch").info(
        "listening on %s:%s (data_dir=%s, scheduler=%s, digest=%s)",
        config.host,
        config.port,
        config.data_dir,
        "off" if scheduler is None else "on",
        config.digest_mode,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if scheduler is not None:
            scheduler.stop()
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
