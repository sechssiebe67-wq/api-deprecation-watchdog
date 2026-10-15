"""HTTP layer: JSON API plus the HTML dashboard.

Built on the standard library's ``http.server`` so the container stays tiny and
dependency-free. Mutating endpoints are protected by an optional bearer token.
"""
import hmac
import json
import logging
import re
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

from .config import Config
from .dashboard import render_dashboard
from .engine import Engine
from .models import (
    ALL_CHANNELS,
    NOTIFY_CHANNELS,
    SEVERITIES,
    SOURCE_KINDS,
    Source,
    canonical_header_name,
)
from .store import Store

log = logging.getLogger("apiwatch.app")

MAX_BODY_BYTES = 64 * 1024
EXPORT_DEFAULT_LIMIT = 1000
EXPORT_MAX_LIMIT = 10000

_GITHUB_HTML_RE = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)
_GITHUB_API_RE = re.compile(
    r"^https?://api\.github\.com/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:/releases)?/?$"
)
_GITLAB_API_RE = re.compile(
    r"^https?://(?:www\.)?gitlab\.com/api/v4/projects/[^/?#]+(?:%2F[^/?#]+)*/releases/?$"
)
_GITLAB_HTML_RE = re.compile(
    r"^https?://(?:www\.)?gitlab\.com/(?P<path>(?!api/)[^?#]+?)(?:\.git)?/?$"
)


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def normalize_github_url(url: str) -> str:
    """Normalize a GitHub repository URL to its releases REST API URL.

    Accepts ``https://github.com/<owner>/<repo>`` (with or without a trailing
    ``.git``) as well as an ``api.github.com/repos/<owner>/<repo>`` URL and
    returns ``https://api.github.com/repos/<owner>/<repo>/releases``. Anything
    that does not look like a GitHub repository URL is returned unchanged.
    """
    for pattern in (_GITHUB_HTML_RE, _GITHUB_API_RE):
        match = pattern.match(url.strip())
        if match:
            return "https://api.github.com/repos/%s/%s/releases" % (
                match.group("owner"),
                match.group("repo"),
            )
    return url


def normalize_gitlab_url(url: str) -> str:
    """Normalize a GitLab project URL to its releases REST API URL.

    Accepts ``https://gitlab.com/<group>/<project>`` (with an optional trailing
    ``.git`` or ``/``; nested subgroups are allowed) and returns
    ``https://gitlab.com/api/v4/projects/<group>%2F<project>/releases``. An
    existing ``https://gitlab.com/api/v4/projects/<id-or-path>/releases`` URL is
    returned unchanged, and anything that does not look like a GitLab project
    URL is returned unchanged too.
    """
    stripped = url.strip()
    if _GITLAB_API_RE.match(stripped):
        return url
    match = _GITLAB_HTML_RE.match(stripped)
    if match:
        path = match.group("path").strip("/")
        if "/" in path:
            return "https://gitlab.com/api/v4/projects/%s/releases" % path.replace(
                "/", "%2F"
            )
    return url


def validate_source_payload(
    payload: dict, default_channels: Optional[List[str]] = None
) -> Source:
    if not isinstance(payload, dict):
        raise ApiError(400, "body must be a JSON object")
    name = (payload.get("name") or "").strip()
    url = (payload.get("url") or "").strip()
    kind = (payload.get("kind") or "").strip().lower()
    if not name:
        raise ApiError(400, "'name' is required")
    if not url.startswith(("http://", "https://")):
        raise ApiError(400, "'url' must be an http(s) URL")
    if kind not in SOURCE_KINDS:
        raise ApiError(400, "'kind' must be one of %s" % ", ".join(SOURCE_KINDS))
    if kind == "github":
        url = normalize_github_url(url)
    elif kind == "gitlab":
        url = normalize_gitlab_url(url)
    versions = payload.get("tracked_versions") or []
    if not isinstance(versions, list) or not all(isinstance(v, str) for v in versions):
        raise ApiError(400, "'tracked_versions' must be a list of strings")
    selector = payload.get("selector")
    if selector is not None and not isinstance(selector, str):
        raise ApiError(400, "'selector' must be a string")
    min_severity = payload.get("min_severity", "info")
    if min_severity is None:
        min_severity = "info"
    if not isinstance(min_severity, str) or min_severity.strip().lower() not in SEVERITIES:
        raise ApiError(
            400, "'min_severity' must be one of %s" % ", ".join(SEVERITIES)
        )
    channels = payload.get("notify_channels")
    if channels is None:
        channels = list(default_channels) if default_channels else [ALL_CHANNELS]
    if not isinstance(channels, list) or not all(
        isinstance(channel, str) for channel in channels
    ):
        raise ApiError(400, "'notify_channels' must be a list of strings")
    normalized_channels = [channel.strip().lower() for channel in channels]
    allowed = set(NOTIFY_CHANNELS) | {ALL_CHANNELS}
    unknown = [channel for channel in normalized_channels if channel not in allowed]
    if unknown:
        raise ApiError(
            400,
            "'notify_channels' entries must be one of %s or 'all'"
            % ", ".join(NOTIFY_CHANNELS),
        )
    notify_webhook_url = payload.get("notify_webhook_url")
    if notify_webhook_url is not None:
        if not isinstance(notify_webhook_url, str) or not notify_webhook_url.strip().startswith(
            ("http://", "https://")
        ):
            raise ApiError(400, "'notify_webhook_url' must be an http(s) URL")
        notify_webhook_url = notify_webhook_url.strip()
    scan_interval = payload.get("scan_interval_seconds")
    if scan_interval is not None:
        # ``bool`` is an ``int`` subclass but is not a meaningful interval.
        if (
            isinstance(scan_interval, bool)
            or not isinstance(scan_interval, int)
            or scan_interval <= 0
        ):
            raise ApiError(
                400, "'scan_interval_seconds' must be a positive integer"
            )
    scan_jitter = payload.get("scan_jitter_seconds")
    if scan_jitter is not None:
        # ``bool`` is an ``int`` subclass but is not a meaningful jitter.
        if (
            isinstance(scan_jitter, bool)
            or not isinstance(scan_jitter, int)
            or scan_jitter < 0
        ):
            raise ApiError(
                400, "'scan_jitter_seconds' must be a non-negative integer"
            )
    max_bytes = payload.get("max_bytes")
    if max_bytes is not None:
        # ``bool`` is an ``int`` subclass but is not a meaningful byte cap.
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
        ):
            raise ApiError(400, "'max_bytes' must be a positive integer")
    retention_days = payload.get("retention_days")
    if retention_days is not None:
        # ``bool`` is an ``int`` subclass but is not a meaningful window.
        if (
            isinstance(retention_days, bool)
            or not isinstance(retention_days, int)
            or retention_days <= 0
        ):
            raise ApiError(400, "'retention_days' must be a positive integer")
    alert_cooldown_seconds = payload.get("alert_cooldown_seconds")
    if alert_cooldown_seconds is not None:
        # ``bool`` is an ``int`` subclass but is not a meaningful window.
        if (
            isinstance(alert_cooldown_seconds, bool)
            or not isinstance(alert_cooldown_seconds, int)
            or alert_cooldown_seconds < 0
        ):
            raise ApiError(
                400, "'alert_cooldown_seconds' must be a non-negative integer"
            )
    headers = payload.get("headers")
    if headers is None:
        headers = {}
    if not isinstance(headers, dict):
        raise ApiError(400, "'headers' must be an object of string values")
    normalized_headers = {}
    for header_name, header_value in headers.items():
        if not isinstance(header_name, str) or not header_name.strip():
            raise ApiError(400, "'headers' names must be non-empty strings")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in header_name):
            raise ApiError(
                400, "'headers' names must not contain control characters or newlines"
            )
        if not isinstance(header_value, str):
            raise ApiError(400, "'headers' values must be strings")
        if "\n" in header_value or "\r" in header_value:
            raise ApiError(400, "'headers' values must not contain newlines")
        normalized_headers[canonical_header_name(header_name)] = header_value
    auth = payload.get("auth")
    if auth is not None:
        if not isinstance(auth, str) or auth.count(":") != 1:
            raise ApiError(400, "'auth' must be a 'user:password' string")
        user, password = auth.split(":", 1)
        if not user or not password:
            raise ApiError(400, "'auth' must have a non-empty user and password")
    extra_keywords = _validate_keyword_list(payload, "extra_keywords")
    suppress_keywords = _validate_keyword_list(payload, "suppress_keywords")
    return Source(
        name=name,
        url=url,
        kind=kind,
        api_name=(payload.get("api_name") or "").strip(),
        tracked_versions=versions,
        enabled=bool(payload.get("enabled", True)),
        selector=selector,
        min_severity=min_severity.strip().lower(),
        notify_channels=normalized_channels,
        notify_webhook_url=notify_webhook_url,
        scan_interval_seconds=scan_interval,
        scan_jitter_seconds=scan_jitter,
        max_bytes=max_bytes,
        retention_days=retention_days,
        alert_cooldown_seconds=alert_cooldown_seconds,
        headers=normalized_headers,
        auth=auth,
        extra_keywords=extra_keywords,
        suppress_keywords=suppress_keywords,
    )


def _validate_keyword_list(payload: dict, field: str) -> List[str]:
    """Validate and normalize an optional override keyword list.

    Missing values (or ``null``) become an empty list. A non-list value or a
    non-string / blank entry is rejected with a ``400`` naming the field. Valid
    terms are trimmed, lower-cased and de-duplicated so the stored vocabulary is
    canonical.
    """
    value = payload.get(field)
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(term, str) for term in value):
        raise ApiError(400, "'%s' must be a list of strings" % field)
    normalized: List[str] = []
    for term in value:
        term = term.strip().lower()
        if not term:
            raise ApiError(400, "'%s' must be a list of strings" % field)
        if term not in normalized:
            normalized.append(term)
    return normalized


class WatchdogHandler(BaseHTTPRequestHandler):
    server_version = "APIWatchdog/0.1"
    protocol_version = "HTTP/1.1"

    # -- helpers ----------------------------------------------------------- #
    @property
    def store(self) -> Store:
        return self.server.store  # type: ignore[attr-defined]

    @property
    def engine(self) -> Engine:
        return self.server.engine  # type: ignore[attr-defined]

    @property
    def config(self) -> Config:
        return self.server.config  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # silence default stderr logging
        log.debug(fmt, *args)

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send_html(self, status: int, markup: str) -> None:
        self._send(status, markup.encode("utf-8"), "text/html; charset=utf-8")

    def _authorized(self) -> bool:
        token = self.config.api_token
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        prefix = "Bearer "
        provided = header[len(prefix):] if header.startswith(prefix) else ""
        if not provided:
            provided = self.headers.get("X-API-Token", "")
        return bool(provided) and hmac.compare_digest(provided, token)

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "invalid Content-Length")
        if length > MAX_BODY_BYTES:
            raise ApiError(413, "request body too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "body must be valid JSON")

    # -- routing ----------------------------------------------------------- #
    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_HEAD(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_DELETE(self) -> None:
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            self._route(method, path, query)
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except Exception as exc:  # pragma: no cover - defensive
            log.exception("unhandled error for %s %s", method, path)
            self._send_json(500, {"error": "internal server error"})

    def _route(self, method: str, path: str, query: dict) -> None:
        if method == "GET" and path == "/healthz":
            self._send_json(200, {"status": "ok"})
            return
        if method == "GET" and path == "/":
            self._dashboard()
            return
        if method == "GET" and path == "/api/stats":
            self._send_json(200, self.store.stats())
            return
        if method == "GET" and path == "/api/export":
            self._export(query)
            return
        if method == "GET" and path == "/api/sources":
            self._send_json(200, {"sources": [s.to_dict() for s in self.store.list_sources()]})
            return
        if method == "POST" and path == "/api/sources":
            self._create_source()
            return
        if method == "GET" and path == "/api/alerts":
            self._list_alerts(query)
            return
        if method == "GET" and path == "/api/entries":
            limit = _int_param(query, "limit", 100)
            offset = _offset_param(query)
            source_id = _int_param(query, "source_id", None)
            entries = self.store.list_entries(
                limit=limit, source_id=source_id, offset=offset
            )
            self._send_json(200, {"entries": [e.to_dict() for e in entries]})
            return
        if method == "GET" and path == "/api/digests":
            limit = _int_param(query, "limit", 100)
            digests = self.store.list_digests(limit=limit)
            self._send_json(200, {"digests": [d.to_dict() for d in digests]})
            return
        if method == "POST" and path == "/api/digest/flush":
            self._flush_digest()
            return
        if method == "POST" and path == "/api/check":
            self._run_check()
            return
        if method == "POST" and path == "/api/prune":
            self._prune()
            return
        if path.startswith("/api/digests/"):
            digest_id = _parse_id(path.rsplit("/", 1)[-1])
            if method == "GET":
                digest = self.store.get_digest(digest_id)
                if digest is None:
                    raise ApiError(404, "digest not found")
                self._send_json(200, digest.to_dict())
                return
        if path.startswith("/api/sources/") and path.endswith("/entries"):
            source_id = _parse_id(path[len("/api/sources/"): -len("/entries")].rstrip("/"))
            if method != "GET":
                raise ApiError(404, "not found")
            if self.store.get_source(source_id) is None:
                raise ApiError(404, "source not found")
            limit = _int_param(query, "limit", 100)
            offset = _offset_param(query)
            entries = self.store.list_entries_for_source(source_id, limit, offset)
            self._send_json(200, {"entries": [e.to_dict() for e in entries]})
            return
        if path.startswith("/api/sources/") and path.endswith("/reset"):
            source_id = _parse_id(
                path[len("/api/sources/"): -len("/reset")].rstrip("/")
            )
            if method != "POST":
                raise ApiError(404, "not found")
            self._reset_source(source_id)
            return
        if path.startswith("/api/sources/"):
            source_id = _parse_id(path.rsplit("/", 1)[-1])
            if method == "DELETE":
                self._delete_source(source_id)
                return
            if method == "GET":
                source = self.store.get_source(source_id)
                if source is None:
                    raise ApiError(404, "source not found")
                self._send_json(200, source.to_dict())
                return
        raise ApiError(404, "not found")

    # -- handlers ---------------------------------------------------------- #
    def _dashboard(self) -> None:
        stats = self.store.stats()
        sources = self.store.list_sources()
        alerts = self.store.list_alerts(limit=50)
        digests = self.store.list_digests(limit=20)
        self._send_html(
            200,
            render_dashboard(
                stats,
                sources,
                alerts,
                digests,
                default_interval=self.config.poll_interval_seconds,
            ),
        )

    def _export(self, query: dict) -> None:
        """Return a read-only snapshot of every stored collection.

        ``?limit=`` caps entries, alerts and digests independently (default
        :data:`EXPORT_DEFAULT_LIMIT`, maximum :data:`EXPORT_MAX_LIMIT`); sources
        are always returned in full so the snapshot is self-describing.
        """
        limit = _export_limit(query)
        payload = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "stats": self.store.stats(),
            "sources": [s.to_dict() for s in self.store.list_sources()],
            "entries": [e.to_dict() for e in self.store.list_entries(limit=limit)],
            "alerts": [a.to_dict() for a in self.store.list_alerts(limit=limit)],
            "digests": [d.to_dict() for d in self.store.list_digests(limit=limit)],
        }
        self._send_json(200, payload)

    def _create_source(self) -> None:
        if not self._authorized():
            raise ApiError(401, "unauthorized")
        source = validate_source_payload(
            self._read_json(), self.config.notify_channels
        )
        created = self.store.add_source(source)
        self._send_json(201, created.to_dict())

    def _delete_source(self, source_id: int) -> None:
        if not self._authorized():
            raise ApiError(401, "unauthorized")
        if not self.store.delete_source(source_id):
            raise ApiError(404, "source not found")
        self._send_json(200, {"deleted": source_id})

    def _reset_source(self, source_id: int) -> None:
        if not self._authorized():
            raise ApiError(401, "unauthorized")
        if self.store.get_source(source_id) is None:
            raise ApiError(404, "source not found")
        self.store.record_source_success(source_id)
        self._send_json(200, self.store.get_source(source_id).to_dict())

    def _list_alerts(self, query: dict) -> None:
        limit = _int_param(query, "limit", 100)
        severity = None
        if query.get("severity"):
            severity = query["severity"][0]
        alerts = self.store.list_alerts(limit=limit, severity=severity)
        self._send_json(200, {"alerts": [a.to_dict() for a in alerts]})

    def _run_check(self) -> None:
        if not self._authorized():
            raise ApiError(401, "unauthorized")
        run_id = self.store.start_check_run()
        result = self.engine.scan_all()
        self.store.finish_check_run(
            run_id, result.sources_checked, result.alerts_created, len(result.errors)
        )
        payload = result.to_dict()
        payload["alerts"] = [a.to_dict() for a in result.alerts]
        self._send_json(200, payload)

    def _flush_digest(self) -> None:
        if not self._authorized():
            raise ApiError(401, "unauthorized")
        digest = self.engine.flush_digest()
        self._send_json(200, {"digest": digest.to_dict() if digest else None})

    def _prune(self) -> None:
        if not self._authorized():
            raise ApiError(401, "unauthorized")
        counts = self.engine.prune()
        self._send_json(
            200,
            {
                "entries_pruned": counts.get("entries_pruned", 0),
                "alerts_pruned": counts.get("alerts_pruned", 0),
            },
        )


def _parse_id(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(400, "invalid id")


def _int_param(query: dict, key: str, default: Optional[int]):
    if key not in query:
        return default
    try:
        return int(query[key][0])
    except (TypeError, ValueError):
        raise ApiError(400, "invalid %s" % key)


def _offset_param(query: dict, default: int = 0) -> int:
    """Resolve the optional non-negative ``?offset=`` or raise ``invalid offset``.

    Omitted means ``0``, so the default page is byte-for-byte unchanged. A
    non-integer (including ``abc``, ``1.5`` or ``true``) or negative value is
    rejected with a ``400`` naming ``offset``.
    """
    if "offset" not in query:
        return default
    try:
        offset = int(query["offset"][0])
    except (TypeError, ValueError):
        raise ApiError(400, "invalid offset")
    if offset < 0:
        raise ApiError(400, "invalid offset")
    return offset


def _export_limit(query: dict) -> int:
    """Resolve the ``?limit=`` for ``/api/export`` or raise ``invalid limit``."""
    if "limit" not in query:
        return EXPORT_DEFAULT_LIMIT
    try:
        limit = int(query["limit"][0])
    except (TypeError, ValueError):
        raise ApiError(400, "invalid limit")
    if limit < 1 or limit > EXPORT_MAX_LIMIT:
        raise ApiError(400, "invalid limit")
    return limit


def create_server(
    store: Store, engine: Engine, config: Config, host: str = None, port: int = None
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host or config.host, port or config.port), WatchdogHandler)
    server.store = store  # type: ignore[attr-defined]
    server.engine = engine  # type: ignore[attr-defined]
    server.config = config  # type: ignore[attr-defined]
    return server
