"""HTTP layer: JSON API plus the HTML dashboard.

Built on the standard library's ``http.server`` so the container stays tiny and
dependency-free. Mutating endpoints are protected by an optional bearer token.
"""
import hmac
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .config import Config
from .dashboard import render_dashboard
from .engine import Engine
from .models import SEVERITIES, SOURCE_KINDS, Source
from .store import Store

log = logging.getLogger("apiwatch.app")

MAX_BODY_BYTES = 64 * 1024


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def validate_source_payload(payload: dict) -> Source:
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
    return Source(
        name=name,
        url=url,
        kind=kind,
        api_name=(payload.get("api_name") or "").strip(),
        tracked_versions=versions,
        enabled=bool(payload.get("enabled", True)),
        selector=selector,
        min_severity=min_severity.strip().lower(),
    )


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
            source_id = _int_param(query, "source_id", None)
            entries = self.store.list_entries(limit=limit, source_id=source_id)
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
        if path.startswith("/api/digests/"):
            digest_id = _parse_id(path.rsplit("/", 1)[-1])
            if method == "GET":
                digest = self.store.get_digest(digest_id)
                if digest is None:
                    raise ApiError(404, "digest not found")
                self._send_json(200, digest.to_dict())
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
        self._send_html(200, render_dashboard(stats, sources, alerts, digests))

    def _create_source(self) -> None:
        if not self._authorized():
            raise ApiError(401, "unauthorized")
        source = validate_source_payload(self._read_json())
        created = self.store.add_source(source)
        self._send_json(201, created.to_dict())

    def _delete_source(self, source_id: int) -> None:
        if not self._authorized():
            raise ApiError(401, "unauthorized")
        if not self.store.delete_source(source_id):
            raise ApiError(404, "source not found")
        self._send_json(200, {"deleted": source_id})

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


def create_server(
    store: Store, engine: Engine, config: Config, host: str = None, port: int = None
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host or config.host, port or config.port), WatchdogHandler)
    server.store = store  # type: ignore[attr-defined]
    server.engine = engine  # type: ignore[attr-defined]
    server.config = config  # type: ignore[attr-defined]
    return server
