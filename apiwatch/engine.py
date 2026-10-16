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
    normalize_alert_title,
    parse_iso8601,
    severity_rank,
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
    entries_capped: int = 0
    alerts_created: int = 0
    alerts_suppressed: int = 0
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
            "entries_capped": self.entries_capped,
            "alerts_created": self.alerts_created,
            "alerts_suppressed": self.alerts_suppressed,
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

        parsed = parsers.parse(
            source.kind,
            body,
            base_url=source.url,
            selector=source.selector,
            baseline=source.openapi_baseline,
            ignore_paths=source.openapi_ignore_paths,
            components=source.statuspage_components,
        )
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
        if source.max_entries is not None:
            # Count-based cap: trim the source to its newest ``max_entries``
            # entries after the scan has persisted everything, so a chatty
            # dependency cannot grow the store past its bound. Only entries are
            # removed; alerts and digests are never touched.
            result.entries_capped += self.store.prune_entries_for_source(
                source.id, source.max_entries
            )
        if notify and new_alerts:
            if self.digest_mode == "digest":
                # Nothing is dispatched immediately: the fan-out cap is a
                # dispatch-time filter and is therefore deferred, while alerts
                # below the notification floor will never be claimed by the
                # digest, so account for them here exactly once (a flush does
                # not re-count them).
                floor_held = sum(
                    1
                    for alert in new_alerts
                    if not self._meets_notification_floor(alert, source)
                )
                if floor_held:
                    self.store.record_suppressed_alerts({source.id: floor_held})
                    result.alerts_suppressed += floor_held
            else:
                selected, capped_out = self._apply_fanout_cap(source, new_alerts)
                if capped_out:
                    # Only the top-N alerts fan out from this scan; the rest
                    # are held back from notification but stay stored with
                    # ``notified = 0`` and remain eligible for a later digest.
                    self.store.record_suppressed_alerts(
                        {source.id: len(capped_out)}
                    )
                    result.alerts_suppressed += len(capped_out)
                if self.digest_mode == "off":
                    result.alerts_suppressed += self.dispatch(selected)
                elif self.digest_mode == "both":
                    result.alerts_suppressed += self.dispatch(selected, mark=False)
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
            result.entries_capped += sub.entries_capped
            result.alerts_created += sub.alerts_created
            result.alerts_suppressed += sub.alerts_suppressed
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

    @staticmethod
    def _effective_cooldown(source: Optional[Source]) -> int:
        """Return a source's effective alert-cooldown window in seconds.

        The window is ``alert_cooldown_seconds`` plus a deterministic,
        source-id-derived jitter offset when ``alert_cooldown_jitter_seconds`` is
        set: ``alert_cooldown_seconds + (source.id % (jitter + 1))``. The offset
        is stable for a given ``(id, jitter)`` pair — never random — so many
        sources that share the same ``alert_cooldown_seconds`` no longer release
        their coalesced alerts on the same tick. A source with no
        ``alert_cooldown_seconds`` (or ``0``) returns ``0`` regardless of the
        jitter, and a source without jitter returns exactly
        ``alert_cooldown_seconds``, so the pre-feature behaviour is preserved
        byte-for-byte.
        """
        if source is None:
            return 0
        cooldown = source.alert_cooldown_seconds or 0
        if cooldown <= 0:
            return 0
        jitter = source.alert_cooldown_jitter_seconds or 0
        if jitter > 0 and source.id is not None:
            cooldown += source.id % (jitter + 1)
        return cooldown

    @staticmethod
    def _meets_notification_floor(alert: Alert, source: Optional[Source]) -> bool:
        """Return whether ``alert`` clears its source's notification floor.

        The effective floor is the per-source ``notify_min_severity`` when set,
        otherwise the source's ``min_severity``. An unknown source (``None``)
        imposes no floor. Alerts below the floor are never fanned out and never
        marked notified, but remain stored.
        """
        if source is None:
            return True
        floor = source.notify_min_severity or source.min_severity
        return meets_floor(alert.severity, floor)

    @staticmethod
    def _apply_fanout_cap(
        source: Optional[Source], alerts: List[Alert]
    ):
        """Split ``alerts`` into the dispatched top-N and the cap-held remainder.

        When the source sets ``max_alerts_per_scan``, at most that many alerts
        may fan out from one scan. The alerts are ordered by severity rank
        (descending) with a *stable* sort, so equal-severity alerts keep their
        creation order and the most dangerous changes always get through. The
        first ``N`` are returned for dispatch and the rest are returned as the
        held-back remainder. A source with no cap (or fewer alerts than the cap)
        is passed through untouched, so the pre-feature behaviour is preserved
        byte-for-byte.
        """
        if source is None or source.max_alerts_per_scan is None:
            return list(alerts), []
        cap = source.max_alerts_per_scan
        if len(alerts) <= cap:
            return list(alerts), []
        ordered = sorted(
            alerts, key=lambda alert: severity_rank(alert.severity), reverse=True
        )
        return ordered[:cap], ordered[cap:]

    def dispatch(self, alerts: List[Alert], mark: bool = True) -> int:
        """Fan alerts out to the notifiers each source is routed to.

        A notifier is only called when at least one alert routes to it, and an
        alert is only marked notified once it has been accepted by at least one
        notifier. This preserves the previous behaviour (a delivered alert is
        marked, a failed notifier is logged and skipped) while honouring the
        per-source ``notify_channels`` routing.

        Three notification-only filters run, in order, before anything is fanned
        out, so an alert must clear all of them to be delivered:

        * the per-source notification floor (``notify_min_severity`` when set,
          otherwise ``min_severity``) holds back alerts whose severity ranks
          below it;
        * ``alert_cooldown_seconds`` holds back every alert from a source that
          alerted within the previous ``N`` seconds (extended by the
          deterministic, id-derived ``alert_cooldown_jitter_seconds`` offset when
          set); and
        * ``dedupe_window_seconds`` holds back an alert whose normalized title
          was already dispatched from the same source within the window.

        A fourth, notification-only filter composes with the others:
        ``escalation_window_seconds`` holds back an alert whose severity was
        already dispatched from the same source within the window, so a source
        that re-announces the *same severity* every few days pages at most once
        per window while a higher-severity alert still escalates through.

        A held-back alert is kept in the store with ``notified = 0`` (so a later
        digest can still pick it up) but is not fanned out now. The last dispatch
        time is read from the source row so the cooldown and the per-title dedupe
        window survive a restart. Returns the number of alerts suppressed by the
        filters so the caller can surface it on :class:`ScanResult`.
        """
        if not alerts or not self.notifiers:
            return 0
        sources = {
            source.id: source
            for source in self.store.list_sources()
            if source.id is not None
        }
        now = self.now()
        counts: Dict[int, int] = {}

        # Notification floor: below-floor alerts are held back first.
        eligible: List[Alert] = []
        for alert in alerts:
            if not self._meets_notification_floor(
                alert, sources.get(alert.source_id)
            ):
                counts[alert.source_id] = counts.get(alert.source_id, 0) + 1
                continue
            eligible.append(alert)

        allowed: Dict[int, bool] = {}
        for alert in eligible:
            source_id = alert.source_id
            if source_id in allowed:
                continue
            source = sources.get(source_id)
            cooldown = self._effective_cooldown(source)
            if not cooldown or source is None or not source.last_alert_at:
                allowed[source_id] = True
                continue
            try:
                elapsed = (now - parse_iso8601(source.last_alert_at)).total_seconds()
            except ValueError:
                # An unparseable timestamp must not silence a source forever.
                allowed[source_id] = True
                continue
            allowed[source_id] = elapsed >= cooldown
        batch = [alert for alert in eligible if allowed.get(alert.source_id, True)]
        for alert in eligible:
            if not allowed.get(alert.source_id, True):
                counts[alert.source_id] = counts.get(alert.source_id, 0) + 1

        # Dedupe window: hold back an alert whose normalized title was already
        # dispatched from the same source within the window. Runs after the
        # floor and cooldown filters so all three compose.
        batch, dedupe_held = self._apply_dedupe_window(batch, sources, now)
        for source_id, count in dedupe_held.items():
            counts[source_id] = counts.get(source_id, 0) + count

        # Escalation window: hold back a repeat of a severity that was already
        # dispatched from the same source within the window. Higher-severity
        # alerts still escalate through, so it composes with the other filters.
        batch, escalation_held = self._apply_escalation_window(batch, sources, now)
        for source_id, count in escalation_held.items():
            counts[source_id] = counts.get(source_id, 0) + count

        suppressed = sum(counts.values())
        if counts:
            self.store.record_suppressed_alerts(counts)
        if not batch:
            return suppressed
        names = self._source_names()
        channels = self._source_channels()
        webhook_urls = self._source_webhook_urls()
        delivered_ids = set()
        for notifier in self.notifiers:
            notifier_batch = [
                alert
                for alert in batch
                if self._routes_to(channels.get(alert.source_id), notifier.name)
            ]
            if not notifier_batch:
                continue
            for group, url in self._notifier_groups(
                notifier, notifier_batch, webhook_urls
            ):
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
        # Remember when each source last alerted so its cooldown window starts
        # now, even across a process restart.
        dispatched_at = now.isoformat(timespec="seconds")
        for source_id in {alert.source_id for alert in batch}:
            self.store.record_source_dispatch(source_id, dispatched_at)
        # Anchor the dedupe window for every title that was actually delivered,
        # so a hold-back only starts once a fan-out has happened. Likewise anchor
        # the escalation window for every severity that was actually delivered.
        severity_times: Dict[int, Dict[str, str]] = {}
        for alert in batch:
            if alert.id is None or alert.id not in delivered_ids:
                continue
            source = sources.get(alert.source_id)
            if source is None:
                continue
            if source.dedupe_window_seconds:
                self.store.record_title_dispatch(
                    alert.source_id, alert.title, dispatched_at
                )
            if source.escalation_window_seconds:
                severity_times.setdefault(alert.source_id, {})[
                    alert.severity
                ] = dispatched_at
        for source_id, mapping in severity_times.items():
            self.store.record_severity_alerts(source_id, mapping)
        if mark and delivered_ids:
            self.store.mark_notified(sorted(delivered_ids))
        return suppressed

    def _apply_dedupe_window(
        self,
        batch: List[Alert],
        sources: Dict[int, Source],
        now: datetime,
    ):
        """Split ``batch`` into deliverable alerts and per-source held counts.

        An alert is held back when its source sets ``dedupe_window_seconds`` and
        the alert's normalized title was already dispatched from that source
        within the window. A title accepted earlier in the same batch also
        anchors the window locally, so two near-identical alerts found in one
        scan yield a single fan-out. Sources without a window (``None`` or ``0``)
        are passed through untouched, which keeps the pre-feature behaviour
        byte-for-byte identical.
        """
        held: Dict[int, int] = {}
        deliverable: List[Alert] = []
        caches: Dict[int, Dict[str, datetime]] = {}
        for alert in batch:
            source = sources.get(alert.source_id)
            window = source.dedupe_window_seconds if source is not None else None
            if not window:
                deliverable.append(alert)
                continue
            cache = caches.get(alert.source_id)
            if cache is None:
                cache = {}
                for title, when in self.store.title_dispatch_times(
                    alert.source_id
                ).items():
                    try:
                        cache[title] = parse_iso8601(when)
                    except (TypeError, ValueError):
                        continue
                caches[alert.source_id] = cache
            key = normalize_alert_title(alert.title)
            prior = cache.get(key)
            if prior is not None and (now - prior).total_seconds() < window:
                held[alert.source_id] = held.get(alert.source_id, 0) + 1
                continue
            cache[key] = now
            deliverable.append(alert)
        return deliverable, held

    def _apply_escalation_window(
        self,
        batch: List[Alert],
        sources: Dict[int, Source],
        now: datetime,
    ):
        """Split ``batch`` into deliverable alerts and per-source held counts.

        An alert is held back when its source sets ``escalation_window_seconds``
        and an alert of the *same* severity was already dispatched from that
        source within the window. This stops a source that re-announces the same
        severity every few days from paging on every re-announcement. Lower- and
        higher-severity alerts are unaffected by a dispatch of another severity:
        a higher-severity alert always escalates through, and a genuinely new
        lower-severity alert is still delivered. Only alerts at or above the
        effective notification floor reach this stage. A severity accepted
        earlier in the same batch anchors the window locally, so several
        same-severity alerts found in one scan yield a single fan-out. Sources
        without a window (``None`` or ``0``) are passed through untouched, which
        keeps the pre-feature behaviour byte-for-byte identical.
        """
        held: Dict[int, int] = {}
        deliverable: List[Alert] = []
        caches: Dict[int, Dict[str, datetime]] = {}
        for alert in batch:
            source = sources.get(alert.source_id)
            window = source.escalation_window_seconds if source is not None else None
            if not window:
                deliverable.append(alert)
                continue
            cache = caches.get(alert.source_id)
            if cache is None:
                cache = {}
                for severity, when in self.store.severity_dispatch_times(
                    alert.source_id
                ).items():
                    try:
                        cache[severity] = parse_iso8601(when)
                    except (TypeError, ValueError):
                        continue
                caches[alert.source_id] = cache
            rank = severity_rank(alert.severity)
            blocked = any(
                severity_rank(severity) == rank
                and (now - when).total_seconds() < window
                for severity, when in cache.items()
            )
            if blocked:
                held[alert.source_id] = held.get(alert.source_id, 0) + 1
                continue
            cache[alert.severity] = now
            deliverable.append(alert)
        return deliverable, held

    def flush_digest(self) -> Optional[Digest]:
        """Build and deliver a digest for the pending alerts, if any.

        Never raises on notifier failure: the digest is logged and left
        undelivered so the failure can be observed without aborting the caller.
        Alerts are grouped by the set of notifiers they route to, so every
        notifier receives a digest containing only its routed alerts.

        The digest honours the same per-source notification floor as immediate
        dispatch: an alert below its source's effective floor is never claimed
        or marked notified and is left pending, so lowering the floor later can
        still surface it.
        """
        alerts = self.store.list_undigested_alerts()
        if not alerts:
            return None
        sources = {
            source.id: source
            for source in self.store.list_sources()
            if source.id is not None
        }
        alerts = [
            alert
            for alert in alerts
            if self._meets_notification_floor(alert, sources.get(alert.source_id))
        ]
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
