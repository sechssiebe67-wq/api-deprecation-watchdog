"""The scanning engine.

Fetches each enabled source, parses it, de-duplicates entries, applies the
relevance filter and fans new alerts out to the configured notifiers.
"""
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, List, Optional, Sequence

from . import digest as digest_builder
from . import parsers
from .fetcher import FetchError, Fetcher
from .models import (
    ALL_CHANNELS,
    Alert,
    Digest,
    Entry,
    Source,
    parse_iso8601,
)
from .notify import NotificationError, Notifier, format_alert
from .relevance import analyze, meets_floor
from .store import Store

log = logging.getLogger("apiwatch.engine")


@dataclass
class ScanResult:
    sources_checked: int = 0
    sources_failed: int = 0
    sources_skipped: int = 0
    sources_truncated: int = 0
    entries_seen: int = 0
    entries_new: int = 0
    alerts_created: int = 0
    alerts: List[Alert] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)
    truncated_sources: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sources_checked": self.sources_checked,
            "sources_failed": self.sources_failed,
            "sources_skipped": self.sources_skipped,
            "sources_truncated": self.sources_truncated,
            "entries_seen": self.entries_seen,
            "entries_new": self.entries_new,
            "alerts_created": self.alerts_created,
            "errors": list(self.errors),
            "truncated_sources": list(self.truncated_sources),
        }


class Engine:
    def __init__(
        self,
        store: Store,
        fetcher: Fetcher,
        notifiers: Optional[Sequence[Notifier]] = None,
        require_version: bool = False,
        digest_mode: str = "off",
        digest_max_alerts: int = 50,
        breaker_threshold: int = 5,
        breaker_cooldown_seconds: int = 1800,
        fetch_max_bytes: int = 2_000_000,
        retention_days: int = 0,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.store = store
        self.fetcher = fetcher
        self.notifiers = list(notifiers or [])
        self.require_version = require_version
        self.digest_mode = digest_mode if digest_mode in ("off", "digest", "both") else "off"
        self.digest_max_alerts = max(1, int(digest_max_alerts))
        self.breaker_threshold = max(1, int(breaker_threshold))
        self.breaker_cooldown_seconds = max(1, int(breaker_cooldown_seconds))
        self.fetch_max_bytes = max(1, int(fetch_max_bytes))
        self.retention_days = max(0, int(retention_days))
        self._clock = clock

    # -- circuit breaker --------------------------------------------------- #
    def now(self) -> datetime:
        value = self._clock() if self._clock is not None else datetime.now(timezone.utc)
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def is_tripped(self, source: Source) -> bool:
        """Return whether ``source`` is currently quarantined by its breaker."""
        if not source.breaker_until:
            return False
        try:
            until = parse_iso8601(source.breaker_until)
        except ValueError:
            return False
        return until > self.now()

    def _register_failure(self, source: Source) -> bool:
        """Record a failed scan, returning ``True`` when this trip the breaker."""
        failures = source.failure_count + 1
        tripped = False
        breaker_until = source.breaker_until
        if failures >= self.breaker_threshold:
            breaker_until = (
                self.now() + timedelta(seconds=self.breaker_cooldown_seconds)
            ).isoformat(timespec="seconds")
            tripped = True
        self.store.record_source_failure(source.id, failures, breaker_until)
        source.failure_count = failures
        source.breaker_until = breaker_until
        return tripped

    def _register_success(self, source: Source) -> None:
        if not source.failure_count and not source.breaker_until:
            return
        self.store.record_source_success(source.id)
        source.failure_count = 0
        source.breaker_until = None

    def scan_source(self, source: Source, notify: bool = True) -> ScanResult:
        result = ScanResult()
        if source.id is None:
            result.errors.append("source has no id")
            return result
        if self.is_tripped(source):
            # Quarantined: report it as skipped rather than failed so a manual
            # check still covers every healthy source without hammering this one.
            result.sources_skipped += 1
            return result
        cap = source.max_bytes if source.max_bytes is not None else self.fetch_max_bytes
        try:
            body, truncated = self.fetcher.fetch(
                source.url,
                headers=source.headers,
                auth=source.auth,
                max_bytes=cap,
                allow_truncated=True,
            )
        except FetchError as exc:
            result.sources_failed += 1
            if self._register_failure(source):
                result.errors.append(
                    "%s: circuit breaker tripped until %s"
                    % (source.name, source.breaker_until)
                )
            result.errors.append("%s: %s" % (source.name, exc))
            return result

        if truncated:
            # The body was clipped at the byte cap. Still parse what arrived so
            # a partial feed degrades gracefully instead of being dropped, but do
            # not treat it as a failure: a large-but-valid feed must not trip the
            # circuit breaker. The signal is surfaced on the result and the store.
            result.sources_truncated += 1
            result.truncated_sources.append(source.name)
            result.errors.append(
                "%s: response truncated to %d bytes" % (source.name, cap)
            )
            log.warning(
                "source %s response truncated at %d bytes; parsing partial body",
                source.name,
                cap,
            )

        parsed = parsers.parse(source.kind, body, base_url=source.url, selector=source.selector)
        result.sources_checked = 1
        result.entries_seen = len(parsed)
        malformed = (
            source.kind == "openapi"
            and body.strip()
            and parsers.openapi_document(body) is None
        )
        if malformed and not truncated:
            # Distinguish malformed input from a legitimate no-op diff: record an
            # error but keep scanning the other sources. A clipped body is never
            # treated as malformed: the missing tail is an artifact of the byte
            # cap, not a broken document, so truncation alone must not increment
            # failure_count or trip the breaker.
            if self._register_failure(source):
                result.errors.append(
                    "%s: circuit breaker tripped until %s"
                    % (source.name, source.breaker_until)
                )
            result.errors.append("%s: malformed OpenAPI input" % source.name)
        else:
            self._register_success(source)
        self.store.record_source_truncation(source.id, truncated)

        versions = source.tracked_versions or []
        new_alerts: List[Alert] = []
        for item in parsed:
            digest = parsers.fingerprint(item.title, item.body, item.published)
            entry = Entry(
                source_id=source.id,
                title=item.title[:500],
                body=item.body[:20000],
                link=item.link,
                published=item.published,
                fingerprint=digest,
            )
            stored = self.store.save_entry(entry)
            if stored is None:
                continue  # already known
            result.entries_new += 1
            verdict = analyze(
                item,
                versions,
                require_version=self.require_version,
                extra_keywords=source.extra_keywords,
                suppress_keywords=source.suppress_keywords,
            )
            if not verdict.relevant:
                continue
            if not meets_floor(verdict.severity, source.min_severity):
                # Below the source's floor: keep the entry, skip the alert.
                continue
            alert = self.store.create_alert(
                Alert(
                    source_id=source.id,
                    entry_id=stored.id,
                    severity=verdict.severity,
                    reason=verdict.reason,
                    matched_terms=verdict.matched_terms,
                    version_hits=verdict.version_hits,
                    title=stored.title,
                    link=stored.link,
                )
            )
            if alert is not None:
                new_alerts.append(alert)

        result.alerts_created = len(new_alerts)
        result.alerts = new_alerts
        if notify and new_alerts:
            if self.digest_mode == "off":
                self.dispatch(new_alerts)
            elif self.digest_mode == "both":
                self.dispatch(new_alerts, mark=False)
            # "digest": hold the alerts back for the next digest flush.
        return result

    def scan_sources(
        self, sources: Sequence[Source], notify: bool = True
    ) -> ScanResult:
        """Scan a specific set of sources, aggregating the per-source results."""
        result = ScanResult()
        for source in sources:
            sub = self.scan_source(source, notify=notify)
            result.sources_checked += sub.sources_checked
            result.sources_failed += sub.sources_failed
            result.sources_skipped += sub.sources_skipped
            result.sources_truncated += sub.sources_truncated
            result.entries_seen += sub.entries_seen
            result.entries_new += sub.entries_new
            result.alerts_created += sub.alerts_created
            result.alerts.extend(sub.alerts)
            result.errors.extend(sub.errors)
            result.truncated_sources.extend(sub.truncated_sources)
            if sub.errors:
                log.warning("scan errors for %s: %s", source.name, sub.errors)
        return result

    def scan_all(self, notify: bool = True) -> ScanResult:
        """Scan every enabled source, regardless of per-source intervals.

        Manual checks (`POST /api/check`) use this so they always cover the whole
        registry; the background scheduler instead scans only the sources whose
        effective interval has elapsed.
        """
        return self.scan_sources(
            self.store.list_sources(enabled_only=True), notify=notify
        )

    def prune(self) -> dict:
        """Prune entries past the effective retention window and report counts.

        The per-source overrides come from each source's stored
        ``retention_days``; sources without one fall back to the global
        ``RETENTION_DAYS``. Sources and digests are never removed.
        """
        overrides = {
            source.id: source.retention_days
            for source in self.store.list_sources()
            if source.id is not None and source.retention_days is not None
        }
        return self.store.prune_entries(self.retention_days, overrides, self.now())

    def dispatch(self, alerts: List[Alert], mark: bool = True) -> None:
        """Fan alerts out to the notifiers each source is routed to.

        A notifier is only called when at least one alert routes to it, and an
        alert is only marked notified once it has been accepted by at least one
        notifier. This preserves the previous behaviour (a delivered alert is
        marked, a failed notifier is logged and skipped) while honouring the
        per-source ``notify_channels`` routing.
        """
        if not alerts or not self.notifiers:
            return
        names = self._source_names()
        channels = self._source_channels()
        webhook_urls = self._source_webhook_urls()
        delivered_ids = set()
        for notifier in self.notifiers:
            batch = [
                alert
                for alert in alerts
                if self._routes_to(channels.get(alert.source_id), notifier.name)
            ]
            if not batch:
                continue
            for group, url in self._notifier_groups(notifier, batch, webhook_urls):
                try:
                    if url is None:
                        notifier.send(group, names)
                    else:
                        notifier.send(group, names, url=url)
                except NotificationError as exc:
                    log.warning("notifier %s failed: %s", notifier.name, exc)
                    continue
                except Exception as exc:  # pragma: no cover - defensive
                    log.warning("notifier %s crashed: %s", notifier.name, exc)
                    continue
                for alert in group:
                    if alert.id is not None:
                        delivered_ids.add(alert.id)
        if mark and delivered_ids:
            self.store.mark_notified(sorted(delivered_ids))

    def flush_digest(self) -> Optional[Digest]:
        """Build and deliver a digest for the pending alerts, if any.

        Never raises on notifier failure: the digest is logged and left
        undelivered so the failure can be observed without aborting the caller.
        Alerts are grouped by the set of notifiers they route to, so every
        notifier receives a digest containing only its routed alerts.
        """
        alerts = self.store.list_undigested_alerts()
        if not alerts:
            return None
        names = self._source_names()
        if not self.notifiers:
            # No external channel is configured; the digest is still recorded.
            stored = self.store.create_digest(
                digest_builder.build_digest(
                    alerts, names, max_alerts=self.digest_max_alerts
                )
            )
            alert_ids = [alert.id for alert in alerts if alert.id is not None]
            self.store.attach_alerts_to_digest(stored.id, alert_ids)
            self.store.mark_digest_delivered(stored.id)
            self.store.mark_notified(alert_ids)
            stored.delivered = True
            return stored

        channels = self._source_channels()
        webhook_urls = self._source_webhook_urls()
        groups: Dict[tuple, List[Alert]] = {}
        for alert in alerts:
            recipients = self._recipients(channels.get(alert.source_id))
            # A per-source webhook URL only matters when the group is routed to
            # the webhook channel; split those groups so each endpoint receives
            # only its own source's alerts.
            target = (
                webhook_urls.get(alert.source_id)
                if "webhook" in recipients
                else None
            )
            groups.setdefault((recipients, target), []).append(alert)

        last: Optional[Digest] = None
        for (recipients, target), group_alerts in groups.items():
            if not recipients:
                # Routed to a channel that is not configured: leave it pending
                # so it is delivered once that notifier is enabled.
                continue
            stored = self.store.create_digest(
                digest_builder.build_digest(
                    group_alerts, names, max_alerts=self.digest_max_alerts
                )
            )
            delivered_ids = set()
            for notifier in self.notifiers:
                if notifier.name not in recipients:
                    continue
                url = (
                    target
                    if getattr(notifier, "supports_url_override", False)
                    else None
                )
                try:
                    if url is None:
                        notifier.send_digest(stored, names)
                    else:
                        notifier.send_digest(stored, names, url=url)
                except NotificationError as exc:
                    log.warning("digest notifier %s failed: %s", notifier.name, exc)
                    continue
                except Exception as exc:  # pragma: no cover - defensive
                    log.warning("digest notifier %s crashed: %s", notifier.name, exc)
                    continue
                for alert in group_alerts:
                    if alert.id is not None:
                        delivered_ids.add(alert.id)
            if delivered_ids:
                alert_ids = sorted(delivered_ids)
                self.store.attach_alerts_to_digest(stored.id, alert_ids)
                self.store.mark_digest_delivered(stored.id)
                self.store.mark_notified(alert_ids)
                stored.delivered = True
            last = stored
        return last

    def _source_names(self) -> Dict[int, str]:
        return {
            source.id: source.name
            for source in self.store.list_sources()
            if source.id is not None
        }

    def _source_channels(self) -> Dict[int, List[str]]:
        return {
            source.id: list(source.notify_channels)
            for source in self.store.list_sources()
            if source.id is not None
        }

    def _source_webhook_urls(self) -> Dict[int, Optional[str]]:
        """Per-source webhook overrides, keyed by source id (``None`` = global)."""
        return {
            source.id: source.notify_webhook_url
            for source in self.store.list_sources()
            if source.id is not None
        }

    def _notifier_groups(
        self,
        notifier: Notifier,
        batch: List[Alert],
        webhook_urls: Dict[int, Optional[str]],
    ):
        """Split a notifier's batch into ``(alerts, url)`` groups.

        Only notifiers that advertise ``supports_url_override`` are grouped by
        their source's webhook URL; every other notifier receives the whole
        batch under a ``None`` URL, preserving the existing behaviour.
        """
        if not getattr(notifier, "supports_url_override", False):
            return [(batch, None)]
        groups: Dict[Optional[str], List[Alert]] = {}
        for alert in batch:
            groups.setdefault(webhook_urls.get(alert.source_id), []).append(alert)
        return [(group, url) for url, group in groups.items()]

    @staticmethod
    def _routes_to(channels: Optional[Sequence[str]], notifier_name: str) -> bool:
        if not channels or ALL_CHANNELS in channels:
            return True
        return notifier_name in channels

    def _recipients(self, channels: Optional[Sequence[str]]) -> frozenset:
        if not channels or ALL_CHANNELS in channels:
            return frozenset(notifier.name for notifier in self.notifiers)
        names = {notifier.name for notifier in self.notifiers}
        return frozenset(channel for channel in channels if channel in names)

    def preview(self, alerts: Sequence[Alert]) -> List[str]:
        names = {source.id: source.name for source in self.store.list_sources()}
        return [format_alert(a, names.get(a.source_id, "")) for a in alerts]


def seed_default_sources(store: Store, sources: Sequence[Source]) -> None:
    existing = {(source.name, source.url) for source in store.list_sources()}
    for source in sources:
        if (source.name, source.url) not in existing:
            store.add_source(source)
