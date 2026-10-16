"""SQLite persistence layer.

A single connection guarded by a lock keeps the store usable from both request
handler threads and the background scheduler. Persistent storage is optional:
passing ``":memory:"`` yields an in-memory database for tests.
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from .models import Alert, Digest, Entry, Source, normalize_alert_title

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    url TEXT NOT NULL,
    kind TEXT NOT NULL,
    api_name TEXT NOT NULL DEFAULT '',
    tracked_versions TEXT NOT NULL DEFAULT '[]',
    enabled INTEGER NOT NULL DEFAULT 1,
    selector TEXT,
    min_severity TEXT NOT NULL DEFAULT 'info',
    notify_min_severity TEXT,
    notify_channels TEXT NOT NULL DEFAULT '["all"]',
    notify_webhook_url TEXT,
    scan_interval_seconds INTEGER,
    scan_jitter_seconds INTEGER,
    max_bytes INTEGER,
    retention_days INTEGER,
    max_entries INTEGER,
    alert_cooldown_seconds INTEGER,
    alert_cooldown_jitter_seconds INTEGER,
    dedupe_window_seconds INTEGER,
    escalation_window_seconds INTEGER,
    last_title_alerts TEXT,
    last_severity_alerts TEXT,
    headers TEXT,
    auth TEXT,
    extra_keywords TEXT NOT NULL DEFAULT '[]',
    suppress_keywords TEXT NOT NULL DEFAULT '[]',
    openapi_baseline TEXT,
    openapi_ignore_paths TEXT NOT NULL DEFAULT '[]',
    failure_count INTEGER NOT NULL DEFAULT 0,
    breaker_until TEXT,
    truncated INTEGER NOT NULL DEFAULT 0,
    last_alert_at TEXT,
    alerts_suppressed INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    published TEXT NOT NULL DEFAULT '',
    fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source_id, fingerprint)
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    entry_id INTEGER NOT NULL,
    severity TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    matched_terms TEXT NOT NULL DEFAULT '[]',
    version_hits TEXT NOT NULL DEFAULT '[]',
    title TEXT NOT NULL DEFAULT '',
    link TEXT NOT NULL DEFAULT '',
    notified INTEGER NOT NULL DEFAULT 0,
    digest_id INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE (entry_id)
);

CREATE TABLE IF NOT EXISTS digests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    alert_count INTEGER NOT NULL DEFAULT 0,
    severity TEXT NOT NULL DEFAULT 'info',
    delivered INTEGER NOT NULL DEFAULT 0,
    body TEXT NOT NULL DEFAULT '',
    subject TEXT NOT NULL DEFAULT '',
    omitted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS check_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    sources_checked INTEGER NOT NULL DEFAULT 0,
    alerts_created INTEGER NOT NULL DEFAULT 0,
    errors INTEGER NOT NULL DEFAULT 0
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        if path != ":memory:":
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()
        # Cumulative entry counts removed since process start, split by the
        # retention (age) pass and the per-source entry-count cap. ``stats``
        # reports their sum as ``entries_pruned``.
        self._entries_pruned_by_retention = 0
        self._entries_pruned_by_cap = 0

    def _migrate(self) -> None:
        """Idempotently evolve pre-existing databases to the current schema."""
        source_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(sources)").fetchall()
        }
        if "min_severity" not in source_columns:
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN min_severity TEXT NOT NULL DEFAULT 'info'"
            )
        if "notify_min_severity" not in source_columns:
            # Old rows keep ``NULL`` so they inherit ``min_severity``.
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN notify_min_severity TEXT"
            )
        if "notify_channels" not in source_columns:
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN notify_channels "
                "TEXT NOT NULL DEFAULT '[\"all\"]'"
            )
        if "scan_interval_seconds" not in source_columns:
            # Old rows keep ``NULL`` so they retain the global poll interval.
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN scan_interval_seconds INTEGER"
            )
        if "scan_jitter_seconds" not in source_columns:
            # Old rows keep ``NULL`` (read back as no jitter, i.e. the default).
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN scan_jitter_seconds INTEGER"
            )
        if "notify_webhook_url" not in source_columns:
            # Old rows keep ``NULL`` so they keep using the global webhook URL.
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN notify_webhook_url TEXT"
            )
        if "headers" not in source_columns:
            # Old rows keep ``NULL`` which is read back as ``{}``.
            self._conn.execute("ALTER TABLE sources ADD COLUMN headers TEXT")
        if "auth" not in source_columns:
            # Old rows keep ``NULL`` which is read back as ``None``.
            self._conn.execute("ALTER TABLE sources ADD COLUMN auth TEXT")
        if "extra_keywords" not in source_columns:
            # Old rows keep ``NULL`` which is read back as ``[]``.
            self._conn.execute("ALTER TABLE sources ADD COLUMN extra_keywords TEXT")
        if "suppress_keywords" not in source_columns:
            # Old rows keep ``NULL`` which is read back as ``[]``.
            self._conn.execute("ALTER TABLE sources ADD COLUMN suppress_keywords TEXT")
        if "openapi_baseline" not in source_columns:
            # Old rows keep ``NULL`` so they keep diffing against the empty spec.
            self._conn.execute("ALTER TABLE sources ADD COLUMN openapi_baseline TEXT")
        if "openapi_ignore_paths" not in source_columns:
            # Old rows default to ``'[]'`` which is read back as ``[]``.
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN openapi_ignore_paths "
                "TEXT NOT NULL DEFAULT '[]'"
            )
        if "failure_count" not in source_columns:
            # Old rows default to ``0`` consecutive failures.
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN failure_count "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "breaker_until" not in source_columns:
            # Old rows keep ``NULL`` so they are not quarantined.
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN breaker_until TEXT"
            )
        if "max_bytes" not in source_columns:
            # Old rows keep ``NULL`` so they fall back to ``FETCH_MAX_BYTES``.
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN max_bytes INTEGER"
            )
        if "truncated" not in source_columns:
            # Old rows default to ``0`` (their last scan was not clipped).
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN truncated "
                "INTEGER NOT NULL DEFAULT 0"
            )
        if "retention_days" not in source_columns:
            # Old rows keep ``NULL`` so they inherit the global ``RETENTION_DAYS``.
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN retention_days INTEGER"
            )
        if "max_entries" not in source_columns:
            # Old rows keep ``NULL`` so they stay unbounded (no entry-count cap).
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN max_entries INTEGER"
            )
        if "alert_cooldown_seconds" not in source_columns:
            # Old rows keep ``NULL`` (read back as no cooldown, i.e. the default).
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN alert_cooldown_seconds INTEGER"
            )
        if "alert_cooldown_jitter_seconds" not in source_columns:
            # Old rows keep ``NULL`` (read back as no cooldown jitter, the default).
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN alert_cooldown_jitter_seconds INTEGER"
            )
        if "dedupe_window_seconds" not in source_columns:
            # Old rows keep ``NULL`` (read back as no dedupe window, the default).
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN dedupe_window_seconds INTEGER"
            )
        if "last_title_alerts" not in source_columns:
            # Old rows keep ``NULL`` which is read back as ``{}`` (no title has
            # been dispatched yet, so nothing is held back).
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN last_title_alerts TEXT"
            )
        if "escalation_window_seconds" not in source_columns:
            # Old rows keep ``NULL`` (read back as no escalation window).
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN escalation_window_seconds INTEGER"
            )
        if "last_severity_alerts" not in source_columns:
            # Old rows keep ``NULL`` which is read back as ``{}`` (no severity
            # has been dispatched yet, so nothing is held back).
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN last_severity_alerts TEXT"
            )
        if "last_alert_at" not in source_columns:
            # Old rows keep ``NULL`` so the first alert is never suppressed.
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN last_alert_at TEXT"
            )
        if "alerts_suppressed" not in source_columns:
            # Old rows default to ``0`` suppressed alerts.
            self._conn.execute(
                "ALTER TABLE sources ADD COLUMN alerts_suppressed "
                "INTEGER NOT NULL DEFAULT 0"
            )

        alert_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(alerts)").fetchall()
        }
        if "digest_id" not in alert_columns:
            self._conn.execute("ALTER TABLE alerts ADD COLUMN digest_id INTEGER")

        digest_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(digests)").fetchall()
        }
        if "subject" not in digest_columns:
            self._conn.execute(
                "ALTER TABLE digests ADD COLUMN subject TEXT NOT NULL DEFAULT ''"
            )
        if "omitted" not in digest_columns:
            self._conn.execute(
                "ALTER TABLE digests ADD COLUMN omitted INTEGER NOT NULL DEFAULT 0"
            )

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- sources ----------------------------------------------------------- #
    def add_source(self, source: Source) -> Source:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO sources
                   (name, url, kind, api_name, tracked_versions, enabled, selector,
                    min_severity, notify_min_severity, notify_channels,
                    notify_webhook_url, scan_interval_seconds,
                    scan_jitter_seconds, max_bytes, retention_days, max_entries,
                    alert_cooldown_seconds, alert_cooldown_jitter_seconds,
                    dedupe_window_seconds,
                    escalation_window_seconds, headers, auth,
                    extra_keywords,
                    suppress_keywords, openapi_baseline, openapi_ignore_paths,
                    failure_count, breaker_until, truncated,
                    last_alert_at, last_severity_alerts, alerts_suppressed,
                    created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source.name,
                    source.url,
                    source.kind,
                    source.api_name,
                    json.dumps(source.tracked_versions),
                    1 if source.enabled else 0,
                    source.selector,
                    source.min_severity,
                    source.notify_min_severity,
                    json.dumps(source.notify_channels),
                    source.notify_webhook_url,
                    source.scan_interval_seconds,
                    source.scan_jitter_seconds,
                    source.max_bytes,
                    source.retention_days,
                    source.max_entries,
                    source.alert_cooldown_seconds,
                    source.alert_cooldown_jitter_seconds,
                    source.dedupe_window_seconds,
                    source.escalation_window_seconds,
                    json.dumps(source.headers or {}),
                    source.auth,
                    json.dumps(source.extra_keywords or []),
                    json.dumps(source.suppress_keywords or []),
                    (
                        json.dumps(source.openapi_baseline)
                        if source.openapi_baseline is not None
                        else None
                    ),
                    json.dumps(source.openapi_ignore_paths or []),
                    source.failure_count,
                    source.breaker_until,
                    1 if source.truncated else 0,
                    source.last_alert_at,
                    json.dumps(source.last_severity_alerts or {}),
                    source.alerts_suppressed,
                    _now(),
                ),
            )
            self._conn.commit()
            source.id = cur.lastrowid
            return self.get_source(source.id)

    def get_source(self, source_id: int) -> Optional[Source]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
        return _row_to_source(row) if row else None

    def list_sources(self, enabled_only: bool = False) -> List[Source]:
        query = "SELECT * FROM sources ORDER BY id"
        if enabled_only:
            query = "SELECT * FROM sources WHERE enabled = 1 ORDER BY id"
        with self._lock:
            rows = self._conn.execute(query).fetchall()
        return [_row_to_source(row) for row in rows]

    def delete_source(self, source_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
            self._conn.execute("DELETE FROM entries WHERE source_id = ?", (source_id,))
            self._conn.execute("DELETE FROM alerts WHERE source_id = ?", (source_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def record_source_failure(
        self, source_id: int, failure_count: int, breaker_until: Optional[str]
    ) -> None:
        """Persist a failed scan's consecutive-failure count and breaker window."""
        with self._lock:
            self._conn.execute(
                "UPDATE sources SET failure_count = ?, breaker_until = ? WHERE id = ?",
                (failure_count, breaker_until, source_id),
            )
            self._conn.commit()

    def record_source_success(self, source_id: int) -> None:
        """Clear a source's failure count and breaker after a successful scan."""
        with self._lock:
            self._conn.execute(
                "UPDATE sources SET failure_count = 0, breaker_until = NULL "
                "WHERE id = ?",
                (source_id,),
            )
            self._conn.commit()

    def record_source_truncation(self, source_id: int, truncated: bool) -> None:
        """Remember whether a source's most recent response body was clipped."""
        with self._lock:
            self._conn.execute(
                "UPDATE sources SET truncated = ? WHERE id = ?",
                (1 if truncated else 0, source_id),
            )
            self._conn.commit()

    def record_source_dispatch(self, source_id: int, when: Optional[str]) -> None:
        """Remember when an alert for ``source_id`` was last fanned out.

        Persisting the timestamp (rather than only holding it in memory) lets the
        per-source alert cooldown survive a restart: a source that just alerted
        stays inside its window even if the process is recycled.
        """
        with self._lock:
            self._conn.execute(
                "UPDATE sources SET last_alert_at = ? WHERE id = ?",
                (when, source_id),
            )
            self._conn.commit()

    def record_title_dispatch(
        self, source_id: int, title: str, when: Optional[str]
    ) -> None:
        """Remember when a normalized alert title was last fanned out.

        ``last_title_alerts`` is a JSON map of normalized title -> ISO-8601
        dispatch time that anchors the per-source dedupe window. Persisting it
        lets the window survive a process restart. ``when=None`` clears the
        entry; the title is normalized with
        :func:`apiwatch.models.normalize_alert_title` so callers may pass a raw
        title.
        """
        key = normalize_alert_title(title)
        with self._lock:
            row = self._conn.execute(
                "SELECT last_title_alerts FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
            if row is None:
                return
            times = _parse_title_alerts(row["last_title_alerts"])
            if when is None:
                times.pop(key, None)
            else:
                times[key] = when
            self._conn.execute(
                "UPDATE sources SET last_title_alerts = ? WHERE id = ?",
                (json.dumps(times), source_id),
            )
            self._conn.commit()

    def title_dispatch_times(self, source_id: int) -> dict:
        """Return the source's normalized-title -> ISO-8601 dispatch-time map.

        Databases created before the column existed and sources that never
        dispatched a title read back as an empty ``{}``; a malformed value is
        treated the same way rather than raising.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT last_title_alerts FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
        if row is None:
            return {}
        return _parse_title_alerts(row["last_title_alerts"])

    def record_severity_alerts(self, source_id: int, mapping: dict) -> None:
        """Merge per-severity dispatch times into ``last_severity_alerts``.

        ``mapping`` maps a severity (``critical``/``warning``/``info``) to the
        ISO-8601 time it was last fanned out. Existing entries for other
        severities are preserved and a repeated severity is overwritten (last
        write wins), so the map anchors the per-source severity escalation
        window across a process restart. An empty ``mapping`` is a no-op.
        """
        if not mapping:
            return
        with self._lock:
            row = self._conn.execute(
                "SELECT last_severity_alerts FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
            if row is None:
                return
            times = _parse_severity_alerts(row["last_severity_alerts"])
            for severity, when in mapping.items():
                times[str(severity)] = str(when)
            self._conn.execute(
                "UPDATE sources SET last_severity_alerts = ? WHERE id = ?",
                (json.dumps(times), source_id),
            )
            self._conn.commit()

    def severity_dispatch_times(self, source_id: int) -> dict:
        """Return the source's severity -> ISO-8601 dispatch-time map.

        Databases created before the column existed and sources that never
        dispatched a severity read back as an empty ``{}``; a malformed value is
        treated the same way rather than raising.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT last_severity_alerts FROM sources WHERE id = ?", (source_id,)
            ).fetchone()
        if row is None:
            return {}
        return _parse_severity_alerts(row["last_severity_alerts"])

    def record_suppressed_alerts(self, counts: dict) -> None:
        """Increment each source's cumulative suppressed-alert counter.

        ``counts`` maps a source id to the number of alerts the notification
        filters held back in one dispatch. The counter is surfaced, aggregated,
        as ``alerts_suppressed`` by :meth:`stats`.
        """
        if not counts:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE sources SET alerts_suppressed = alerts_suppressed + ? "
                "WHERE id = ?",
                [(count, source_id) for source_id, count in counts.items()],
            )
            self._conn.commit()

    # -- entries ----------------------------------------------------------- #
    def save_entry(self, entry: Entry) -> Optional[Entry]:
        """Insert an entry, returning it when new and ``None`` when duplicate."""
        with self._lock:
            try:
                cur = self._conn.execute(
                    """INSERT INTO entries
                       (source_id, title, body, link, published, fingerprint, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        entry.source_id,
                        entry.title,
                        entry.body,
                        entry.link,
                        entry.published,
                        entry.fingerprint,
                        _now(),
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                return None
            entry.id = cur.lastrowid
        return self.get_entry(entry.id)

    def get_entry(self, entry_id: int) -> Optional[Entry]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM entries WHERE id = ?", (entry_id,)
            ).fetchone()
        return _row_to_entry(row) if row else None

    def list_entries(
        self,
        limit: int = 100,
        source_id: Optional[int] = None,
        offset: int = 0,
    ) -> List[Entry]:
        """Return entries newest first, skipping the first ``offset`` of them.

        ``offset`` defaults to ``0``, so callers that omit it see exactly the
        pre-pagination result. It composes with ``limit``: ``offset`` entries are
        skipped before the page is capped.
        """
        query = "SELECT * FROM entries"
        params: tuple = ()
        if source_id is not None:
            query += " WHERE source_id = ?"
            params = (source_id,)
        query += " ORDER BY id DESC LIMIT ? OFFSET ?"
        params += (limit, offset)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_row_to_entry(row) for row in rows]

    def list_entries_for_source(
        self, source_id: int, limit: int = 100, offset: int = 0
    ) -> List[Entry]:
        """Return one source's entries, newest first by ``created_at``.

        Ties on ``created_at`` (which has second resolution) are broken by ``id``
        descending, so the order is deterministic. An unknown source id simply
        yields an empty list; the HTTP layer decides whether that is a ``404``.
        The optional ``offset`` skips that many newest entries before ``limit``
        caps the page; it defaults to ``0`` and preserves the previous result.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM entries
                   WHERE source_id = ?
                   ORDER BY created_at DESC, id DESC
                   LIMIT ? OFFSET ?""",
                (source_id, limit, offset),
            ).fetchall()
        return [_row_to_entry(row) for row in rows]

    def prune_entries(
        self,
        retention_days: int = 0,
        source_overrides: Optional[dict] = None,
        now: Optional[datetime] = None,
    ) -> dict:
        """Delete entries older than the effective retention window per source.

        The window for a source is, in order of precedence, an explicit
        ``source_overrides[source_id]`` value, the source's own stored
        ``retention_days``, or the global ``retention_days`` argument. A window
        of ``None`` or ``0`` disables pruning for that source. Alerts whose
        ``entry_id`` was pruned are removed with their entry; ``sources`` and
        ``digests`` are never touched, so the registry and the aggregate history
        survive a prune. Returns ``{"entries_pruned": N, "alerts_pruned": M}``.
        """
        overrides = source_overrides or {}
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        moment = moment.astimezone(timezone.utc)
        entries_pruned = 0
        alerts_pruned = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, retention_days FROM sources"
            ).fetchall()
            for row in rows:
                source_id = row["id"]
                if source_id in overrides:
                    window = overrides[source_id]
                elif row["retention_days"] is not None:
                    window = row["retention_days"]
                else:
                    window = retention_days
                if not window or window <= 0:
                    continue
                cutoff = (moment - timedelta(days=window)).isoformat(
                    timespec="seconds"
                )
                stale = [
                    entry["id"]
                    for entry in self._conn.execute(
                        "SELECT id FROM entries WHERE source_id = ? AND created_at < ?",
                        (source_id, cutoff),
                    ).fetchall()
                ]
                if not stale:
                    continue
                placeholders = ",".join("?" for _ in stale)
                cur = self._conn.execute(
                    "DELETE FROM entries WHERE id IN (%s)" % placeholders, stale
                )
                entries_pruned += cur.rowcount
                acur = self._conn.execute(
                    "DELETE FROM alerts WHERE entry_id IN (%s)" % placeholders,
                    stale,
                )
                alerts_pruned += acur.rowcount
            self._conn.commit()
            self._entries_pruned_by_retention += entries_pruned
        return {"entries_pruned": entries_pruned, "alerts_pruned": alerts_pruned}

    def prune_entries_for_source(
        self, source_id: int, max_entries: Optional[int]
    ) -> int:
        """Delete the oldest entries for one source beyond its count cap.

        The newest ``max_entries`` entries, ordered by ``created_at DESC,
        id DESC`` (the same order :meth:`list_entries_for_source` uses), are
        kept and every older row is removed. Only the ``entries`` table is
        touched: alerts and digests are **never** deleted, so an alert whose
        entry was dropped still renders with its own stored title and link, and
        ``GET /api/digests`` is unaffected. ``max_entries`` of ``None`` or ``<= 0``
        disables the cap and deletes nothing. Returns the number of entries
        deleted (also accumulated for ``stats``).
        """
        if max_entries is None or max_entries <= 0:
            return 0
        with self._lock:
            stale = [
                row["id"]
                for row in self._conn.execute(
                    """SELECT id FROM entries
                       WHERE source_id = ?
                       ORDER BY created_at DESC, id DESC
                       LIMIT -1 OFFSET ?""",
                    (source_id, max_entries),
                ).fetchall()
            ]
            if not stale:
                return 0
            placeholders = ",".join("?" for _ in stale)
            cur = self._conn.execute(
                "DELETE FROM entries WHERE id IN (%s)" % placeholders, stale
            )
            self._conn.commit()
            self._entries_pruned_by_cap += cur.rowcount
            return cur.rowcount

    # -- alerts ------------------------------------------------------------ #
    def create_alert(self, alert: Alert) -> Optional[Alert]:
        with self._lock:
            try:
                cur = self._conn.execute(
                    """INSERT INTO alerts
                       (source_id, entry_id, severity, reason, matched_terms,
                        version_hits, title, link, notified, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        alert.source_id,
                        alert.entry_id,
                        alert.severity,
                        alert.reason,
                        json.dumps(alert.matched_terms),
                        json.dumps(alert.version_hits),
                        alert.title,
                        alert.link,
                        1 if alert.notified else 0,
                        _now(),
                    ),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                return None
            alert.id = cur.lastrowid
        return self.get_alert(alert.id)

    def get_alert(self, alert_id: int) -> Optional[Alert]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM alerts WHERE id = ?", (alert_id,)
            ).fetchone()
        return _row_to_alert(row) if row else None

    def list_alerts(
        self,
        limit: int = 100,
        severity: Optional[str] = None,
        notified: Optional[bool] = None,
    ) -> List[Alert]:
        clauses = []
        params: list = []
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        if notified is not None:
            clauses.append("notified = ?")
            params.append(1 if notified else 0)
        query = "SELECT * FROM alerts"
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_row_to_alert(row) for row in rows]

    def mark_notified(self, alert_ids: List[int]) -> None:
        if not alert_ids:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE alerts SET notified = 1 WHERE id = ?",
                [(alert_id,) for alert_id in alert_ids],
            )
            self._conn.commit()

    # -- digests ----------------------------------------------------------- #
    def create_digest(self, digest: Digest) -> Digest:
        """Persist a digest row.

        Member alerts are *not* attached here: only a successfully delivered
        digest claims its alerts (see :meth:`attach_alerts_to_digest`), so a
        failed flush leaves the alerts pending for the next attempt.
        """
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO digests
                   (created_at, alert_count, severity, delivered, body, subject, omitted)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    digest.created_at or _now(),
                    digest.alert_count,
                    digest.severity,
                    1 if digest.delivered else 0,
                    digest.body,
                    digest.subject,
                    digest.omitted,
                ),
            )
            digest_id = cur.lastrowid
            self._conn.commit()
        return self.get_digest(digest_id)

    def attach_alerts_to_digest(self, digest_id: int, alert_ids: List[int]) -> None:
        """Mark ``alert_ids`` as consumed by an already delivered digest."""
        if not alert_ids:
            return
        with self._lock:
            self._conn.executemany(
                "UPDATE alerts SET digest_id = ? WHERE id = ?",
                [(digest_id, alert_id) for alert_id in alert_ids],
            )
            self._conn.commit()

    def get_digest(self, digest_id: int) -> Optional[Digest]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM digests WHERE id = ?", (digest_id,)
            ).fetchone()
        return _row_to_digest(row) if row else None

    def list_digests(self, limit: int = 100) -> List[Digest]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM digests ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [_row_to_digest(row) for row in rows]

    def mark_digest_delivered(self, digest_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE digests SET delivered = 1 WHERE id = ?", (digest_id,)
            )
            self._conn.commit()

    def list_undigested_alerts(self) -> List[Alert]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM alerts
                   WHERE notified = 0 AND digest_id IS NULL
                   ORDER BY id"""
            ).fetchall()
        return [_row_to_alert(row) for row in rows]

    # -- check runs / stats ------------------------------------------------ #
    def start_check_run(self) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO check_runs (started_at) VALUES (?)", (_now(),)
            )
            self._conn.commit()
            return cur.lastrowid

    def finish_check_run(
        self, run_id: int, sources_checked: int, alerts_created: int, errors: int
    ) -> None:
        with self._lock:
            self._conn.execute(
                """UPDATE check_runs
                   SET finished_at = ?, sources_checked = ?, alerts_created = ?, errors = ?
                   WHERE id = ?""",
                (_now(), sources_checked, alerts_created, errors, run_id),
            )
            self._conn.commit()

    def stats(self) -> dict:
        with self._lock:
            sources = self._conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            enabled = self._conn.execute(
                "SELECT COUNT(*) FROM sources WHERE enabled = 1"
            ).fetchone()[0]
            # ISO-8601 UTC strings with the same format compare lexicographically,
            # so a string ``>`` correctly selects breakers that have not expired.
            tripped = self._conn.execute(
                "SELECT COUNT(*) FROM sources "
                "WHERE breaker_until IS NOT NULL AND breaker_until > ?",
                (_now(),),
            ).fetchone()[0]
            truncated = self._conn.execute(
                "SELECT COUNT(*) FROM sources WHERE truncated = 1"
            ).fetchone()[0]
            entries = self._conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
            alerts = self._conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0]
            critical = self._conn.execute(
                "SELECT COUNT(*) FROM alerts WHERE severity = 'critical'"
            ).fetchone()[0]
            warning = self._conn.execute(
                "SELECT COUNT(*) FROM alerts WHERE severity = 'warning'"
            ).fetchone()[0]
            digests = self._conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0]
            pending = self._conn.execute(
                "SELECT COUNT(*) FROM alerts WHERE notified = 0 AND digest_id IS NULL"
            ).fetchone()[0]
            suppressed = self._conn.execute(
                "SELECT COALESCE(SUM(alerts_suppressed), 0) FROM sources"
            ).fetchone()[0]
            last_run = self._conn.execute(
                "SELECT * FROM check_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            entries_pruned = (
                self._entries_pruned_by_retention + self._entries_pruned_by_cap
            )
        return {
            "sources": sources,
            "sources_enabled": enabled,
            "sources_tripped": tripped,
            "sources_truncated": truncated,
            "entries": entries,
            "entries_pruned": entries_pruned,
            "alerts": alerts,
            "alerts_critical": critical,
            "alerts_warning": warning,
            "digests": digests,
            "alerts_pending_digest": pending,
            "alerts_suppressed": suppressed,
            "last_check": dict(last_run) if last_run else None,
        }


# --------------------------------------------------------------------------- #
# Row mappers
# --------------------------------------------------------------------------- #
def _parse_title_alerts(raw) -> dict:
    """Decode the ``last_title_alerts`` JSON column into a dict.

    ``NULL``, malformed JSON and JSON values that are not objects all read back
    as an empty dict, so a legacy row can never crash a dispatch.
    """
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _parse_json_object(raw):
    """Decode a JSON object column, returning ``None`` when unset or malformed.

    Used for the optional ``openapi_baseline`` payload, so a legacy row (or a
    value that is not a JSON object) can never crash a scan.
    """
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _parse_string_list(raw) -> list:
    """Decode a JSON list-of-strings column, defaulting to ``[]``.

    ``NULL``, malformed JSON and JSON values that are not lists all read back as
    an empty list; non-string or blank entries are dropped so a legacy row can
    never poison the parser.
    """
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(value, list):
        return []
    normalized = []
    for entry in value:
        if isinstance(entry, str) and entry.strip():
            entry = entry.strip()
            if entry not in normalized:
                normalized.append(entry)
    return normalized


def _parse_severity_alerts(raw) -> dict:
    """Decode the ``last_severity_alerts`` JSON column into a dict.

    ``NULL``, malformed JSON and JSON values that are not objects all read back
    as an empty dict, so a legacy row can never crash a dispatch. Keys and
    values are coerced to strings so the map is always ``{severity: iso8601}``.
    """
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(value, dict):
        return {}
    return {str(severity): str(when) for severity, when in value.items()}


def _row_to_source(row: sqlite3.Row) -> Source:
    return Source(
        id=row["id"],
        name=row["name"],
        url=row["url"],
        kind=row["kind"],
        api_name=row["api_name"],
        tracked_versions=json.loads(row["tracked_versions"] or "[]"),
        enabled=bool(row["enabled"]),
        selector=row["selector"],
        min_severity=row["min_severity"],
        notify_min_severity=row["notify_min_severity"],
        notify_channels=json.loads(row["notify_channels"] or '["all"]'),
        notify_webhook_url=row["notify_webhook_url"],
        scan_interval_seconds=row["scan_interval_seconds"],
        scan_jitter_seconds=row["scan_jitter_seconds"],
        max_bytes=row["max_bytes"],
        retention_days=row["retention_days"],
        max_entries=row["max_entries"],
        alert_cooldown_seconds=row["alert_cooldown_seconds"],
        alert_cooldown_jitter_seconds=row["alert_cooldown_jitter_seconds"],
        dedupe_window_seconds=row["dedupe_window_seconds"],
        escalation_window_seconds=row["escalation_window_seconds"],
        headers=json.loads(row["headers"] or "{}"),
        auth=row["auth"],
        extra_keywords=json.loads(row["extra_keywords"] or "[]"),
        suppress_keywords=json.loads(row["suppress_keywords"] or "[]"),
        openapi_baseline=_parse_json_object(row["openapi_baseline"]),
        openapi_ignore_paths=_parse_string_list(row["openapi_ignore_paths"]),
        created_at=row["created_at"],
        failure_count=int(row["failure_count"] or 0),
        breaker_until=row["breaker_until"],
        truncated=bool(row["truncated"]),
        last_alert_at=row["last_alert_at"],
        last_severity_alerts=_parse_severity_alerts(row["last_severity_alerts"]),
        alerts_suppressed=int(row["alerts_suppressed"] or 0),
    )


def _row_to_entry(row: sqlite3.Row) -> Entry:
    return Entry(
        id=row["id"],
        source_id=row["source_id"],
        title=row["title"],
        body=row["body"],
        link=row["link"],
        published=row["published"],
        fingerprint=row["fingerprint"],
        created_at=row["created_at"],
    )


def _row_to_alert(row: sqlite3.Row) -> Alert:
    return Alert(
        id=row["id"],
        source_id=row["source_id"],
        entry_id=row["entry_id"],
        severity=row["severity"],
        reason=row["reason"],
        matched_terms=json.loads(row["matched_terms"] or "[]"),
        version_hits=json.loads(row["version_hits"] or "[]"),
        title=row["title"],
        link=row["link"],
        notified=bool(row["notified"]),
        digest_id=row["digest_id"],
        created_at=row["created_at"],
    )


def _row_to_digest(row: sqlite3.Row) -> Digest:
    return Digest(
        id=row["id"],
        created_at=row["created_at"],
        alert_count=row["alert_count"],
        severity=row["severity"],
        delivered=bool(row["delivered"]),
        body=row["body"],
        subject=row["subject"],
        omitted=row["omitted"],
    )
