"""SQLite persistence layer.

A single connection guarded by a lock keeps the store usable from both request
handler threads and the background scheduler. Persistent storage is optional:
passing ``":memory:"`` yields an in-memory database for tests.
"""
import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from typing import List, Optional

from .models import Alert, Digest, Entry, Source

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
    notify_channels TEXT NOT NULL DEFAULT '["all"]',
    notify_webhook_url TEXT,
    scan_interval_seconds INTEGER,
    headers TEXT,
    auth TEXT,
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
                    min_severity, notify_channels, notify_webhook_url,
                    scan_interval_seconds, headers, auth, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source.name,
                    source.url,
                    source.kind,
                    source.api_name,
                    json.dumps(source.tracked_versions),
                    1 if source.enabled else 0,
                    source.selector,
                    source.min_severity,
                    json.dumps(source.notify_channels),
                    source.notify_webhook_url,
                    source.scan_interval_seconds,
                    json.dumps(source.headers or {}),
                    source.auth,
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

    def list_entries(self, limit: int = 100, source_id: Optional[int] = None) -> List[Entry]:
        query = "SELECT * FROM entries"
        params: tuple = ()
        if source_id is not None:
            query += " WHERE source_id = ?"
            params = (source_id,)
        query += " ORDER BY id DESC LIMIT ?"
        params += (limit,)
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [_row_to_entry(row) for row in rows]

    def list_entries_for_source(self, source_id: int, limit: int = 100) -> List[Entry]:
        """Return one source's entries, newest first by ``created_at``.

        Ties on ``created_at`` (which has second resolution) are broken by ``id``
        descending, so the order is deterministic. An unknown source id simply
        yields an empty list; the HTTP layer decides whether that is a ``404``.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM entries
                   WHERE source_id = ?
                   ORDER BY created_at DESC, id DESC
                   LIMIT ?""",
                (source_id, limit),
            ).fetchall()
        return [_row_to_entry(row) for row in rows]

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
            last_run = self._conn.execute(
                "SELECT * FROM check_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {
            "sources": sources,
            "sources_enabled": enabled,
            "entries": entries,
            "alerts": alerts,
            "alerts_critical": critical,
            "alerts_warning": warning,
            "digests": digests,
            "alerts_pending_digest": pending,
            "last_check": dict(last_run) if last_run else None,
        }


# --------------------------------------------------------------------------- #
# Row mappers
# --------------------------------------------------------------------------- #
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
        notify_channels=json.loads(row["notify_channels"] or '["all"]'),
        notify_webhook_url=row["notify_webhook_url"],
        scan_interval_seconds=row["scan_interval_seconds"],
        headers=json.loads(row["headers"] or "{}"),
        auth=row["auth"],
        created_at=row["created_at"],
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
