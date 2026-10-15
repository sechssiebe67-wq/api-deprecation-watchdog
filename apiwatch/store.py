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

from .models import Alert, Entry, Source

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
    created_at TEXT NOT NULL,
    UNIQUE (entry_id)
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
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- sources ----------------------------------------------------------- #
    def add_source(self, source: Source) -> Source:
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO sources
                   (name, url, kind, api_name, tracked_versions, enabled, selector, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source.name,
                    source.url,
                    source.kind,
                    source.api_name,
                    json.dumps(source.tracked_versions),
                    1 if source.enabled else 0,
                    source.selector,
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
        created_at=row["created_at"],
    )
