"""The scanning engine.

Fetches each enabled source, parses it, de-duplicates entries, applies the
relevance filter and fans new alerts out to the configured notifiers.
"""
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from . import digest as digest_builder
from . import parsers
from .fetcher import FetchError, Fetcher
from .models import ALL_CHANNELS, Alert, Digest, Entry, Source
from .notify import NotificationError, Notifier, format_alert
from .relevance import analyze, meets_floor
from .store import Store

log = logging.getLogger("apiwatch.engine")


@dataclass
class ScanResult:
    sources_checked: int = 0
    sources_failed: int = 0
    entries_seen: int = 0
    entries_new: int = 0
    alerts_created: int = 0
    alerts: List[Alert] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sources_checked": self.sources_checked,
            "sources_failed": self.sources_failed,
            "entries_seen": self.entries_seen,
            "entries_new": self.entries_new,
            "alerts_created": self.alerts_created,
            "errors": list(self.errors),
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
    ) -> None:
        self.store = store
        self.fetcher = fetcher
        self.notifiers = list(notifiers or [])
        self.require_version = require_version
        self.digest_mode = digest_mode if digest_mode in ("off", "digest", "both") else "off"
        self.digest_max_alerts = max(1, int(digest_max_alerts))

    def scan_source(self, source: Source, notify: bool = True) -> ScanResult:
        result = ScanResult()
        if source.id is None:
            result.errors.append("source has no id")
            return result
        try:
            body = self.fetcher.fetch(source.url)
        except FetchError as exc:
            result.sources_failed += 1
            result.errors.append("%s: %s" % (source.name, exc))
            return result

        parsed = parsers.parse(source.kind, body, base_url=source.url, selector=source.selector)
        result.sources_checked = 1
        result.entries_seen = len(parsed)
        if (
            source.kind == "openapi"
            and body.strip()
            and parsers.openapi_document(body) is None
        ):
            # Distinguish malformed input from a legitimate no-op diff: record an
            # error but keep scanning the other sources.
            result.errors.append("%s: malformed OpenAPI input" % source.name)

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
            verdict = analyze(item, versions, require_version=self.require_version)
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
            result.entries_seen += sub.entries_seen
            result.entries_new += sub.entries_new
            result.alerts_created += sub.alerts_created
            result.alerts.extend(sub.alerts)
            result.errors.extend(sub.errors)
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
        delivered_ids = set()
        for notifier in self.notifiers:
            batch = [
                alert
                for alert in alerts
                if self._routes_to(channels.get(alert.source_id), notifier.name)
            ]
            if not batch:
                continue
            try:
                notifier.send(batch, names)
            except NotificationError as exc:
                log.warning("notifier %s failed: %s", notifier.name, exc)
                continue
            except Exception as exc:  # pragma: no cover - defensive
                log.warning("notifier %s crashed: %s", notifier.name, exc)
                continue
            for alert in batch:
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
        groups: Dict[frozenset, List[Alert]] = {}
        for alert in alerts:
            recipients = self._recipients(channels.get(alert.source_id))
            groups.setdefault(recipients, []).append(alert)

        last: Optional[Digest] = None
        for recipients, group_alerts in groups.items():
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
                try:
                    notifier.send_digest(stored, names)
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
