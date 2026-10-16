"""Domain models for API Deprecation Watchdog.

Plain dataclasses so the core logic stays dependency-free and easy to test.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

SOURCE_KINDS = ("rss", "html", "json", "statuspage", "github", "gitlab", "openapi")
SEVERITIES = ("critical", "warning", "info")
NOTIFY_CHANNELS = ("console", "slack", "email", "webhook")
ALL_CHANNELS = "all"


def parse_iso8601(value: str) -> datetime:
    """Parse an ISO-8601 timestamp into a timezone-aware UTC ``datetime``.

    Accepts the ``+00:00`` form written by the store as well as a trailing
    ``Z`` (which ``datetime.fromisoformat`` rejects before Python 3.11). Naive
    values are assumed to be UTC.
    """
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC ``datetime``."""
    return datetime.now(timezone.utc)


def normalize_alert_title(title: str) -> str:
    """Return a canonical key for alert-title de-duplication.

    Leading/trailing whitespace is trimmed, every run of internal whitespace is
    collapsed to a single space and the result is case-folded, so titles that
    differ only in spacing or case share a key.
    """
    if title is None:
        return ""
    return " ".join(str(title).split()).casefold()


def canonical_header_name(name: str) -> str:
    """Return the canonical ``Title-Case`` form of an HTTP header name.

    ``user-agent`` and ``User-Agent`` both map to ``User-Agent`` so the two
    spellings cannot be stored as separate keys.
    """
    return "-".join(part.capitalize() for part in name.strip().split("-"))


@dataclass
class Source:
    name: str
    url: str
    kind: str
    api_name: str = ""
    tracked_versions: List[str] = field(default_factory=list)
    enabled: bool = True
    selector: Optional[str] = None
    min_severity: str = "info"
    notify_min_severity: Optional[str] = None
    notify_channels: List[str] = field(default_factory=lambda: [ALL_CHANNELS])
    notify_webhook_url: Optional[str] = None
    scan_interval_seconds: Optional[int] = None
    scan_jitter_seconds: Optional[int] = None
    max_bytes: Optional[int] = None
    retention_days: Optional[int] = None
    alert_cooldown_seconds: Optional[int] = None
    dedupe_window_seconds: Optional[int] = None
    headers: Dict[str, str] = field(default_factory=dict)
    auth: Optional[str] = None
    extra_keywords: List[str] = field(default_factory=list)
    suppress_keywords: List[str] = field(default_factory=list)
    id: Optional[int] = None
    created_at: Optional[str] = None
    failure_count: int = 0
    breaker_until: Optional[str] = None
    truncated: bool = False
    last_alert_at: Optional[str] = None
    alerts_suppressed: int = 0

    def __post_init__(self) -> None:
        if self.min_severity not in SEVERITIES:
            raise ValueError("min_severity must be one of %s" % ", ".join(SEVERITIES))
        if self.notify_min_severity is not None:
            # ``None`` means "inherit min_severity"; anything else must be a
            # severity, normalized to the canonical lowercase spelling.
            if not isinstance(self.notify_min_severity, str):
                raise ValueError(
                    "notify_min_severity must be one of %s" % ", ".join(SEVERITIES)
                )
            normalized_floor = self.notify_min_severity.strip().lower()
            if normalized_floor not in SEVERITIES:
                raise ValueError(
                    "notify_min_severity must be one of %s" % ", ".join(SEVERITIES)
                )
            self.notify_min_severity = normalized_floor
        if self.notify_webhook_url is not None:
            if (
                not isinstance(self.notify_webhook_url, str)
                or not self.notify_webhook_url.strip().startswith(("http://", "https://"))
            ):
                raise ValueError("notify_webhook_url must be an http(s) URL")
            self.notify_webhook_url = self.notify_webhook_url.strip()
        interval = self.scan_interval_seconds
        if interval is not None:
            # ``bool`` is an ``int`` subclass but is not a meaningful interval.
            if isinstance(interval, bool) or not isinstance(interval, int) or interval <= 0:
                raise ValueError("scan_interval_seconds must be a positive integer")
        jitter = self.scan_jitter_seconds
        if jitter is not None:
            # ``bool`` is an ``int`` subclass but is not a meaningful jitter.
            if isinstance(jitter, bool) or not isinstance(jitter, int) or jitter < 0:
                raise ValueError(
                    "scan_jitter_seconds must be a non-negative integer"
                )
            # ``0`` and ``None`` both mean "no jitter"; normalising keeps a single
            # canonical representation in the store, the API and the scheduler.
            if jitter == 0:
                self.scan_jitter_seconds = None
        limit = self.max_bytes
        if limit is not None:
            # ``bool`` is an ``int`` subclass but is not a meaningful byte cap.
            if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
                raise ValueError("max_bytes must be a positive integer")
        retention = self.retention_days
        if retention is not None:
            # ``bool`` is an ``int`` subclass but is not a meaningful window.
            if isinstance(retention, bool) or not isinstance(retention, int) or retention <= 0:
                raise ValueError("retention_days must be a positive integer")
        cooldown = self.alert_cooldown_seconds
        if cooldown is not None:
            # ``bool`` is an ``int`` subclass but is not a meaningful window.
            if isinstance(cooldown, bool) or not isinstance(cooldown, int) or cooldown < 0:
                raise ValueError(
                    "alert_cooldown_seconds must be a non-negative integer"
                )
        dedupe = self.dedupe_window_seconds
        if dedupe is not None:
            # ``bool`` is an ``int`` subclass but is not a meaningful window.
            if isinstance(dedupe, bool) or not isinstance(dedupe, int) or dedupe < 0:
                raise ValueError(
                    "dedupe_window_seconds must be a non-negative integer"
                )
        if (
            isinstance(self.alerts_suppressed, bool)
            or not isinstance(self.alerts_suppressed, int)
            or self.alerts_suppressed < 0
        ):
            raise ValueError("alerts_suppressed must be a non-negative integer")
        self.truncated = bool(self.truncated)
        channels = self.notify_channels
        if channels is None:
            channels = [ALL_CHANNELS]
        if not isinstance(channels, (list, tuple)):
            raise ValueError("notify_channels must be a list of channel names")
        normalized: List[str] = []
        for channel in channels:
            if not isinstance(channel, str):
                raise ValueError("notify_channels must be a list of channel names")
            channel = channel.strip().lower()
            if channel != ALL_CHANNELS and channel not in NOTIFY_CHANNELS:
                raise ValueError(
                    "notify_channels entries must be one of %s or 'all'"
                    % ", ".join(NOTIFY_CHANNELS)
                )
            if channel not in normalized:
                normalized.append(channel)
        # An empty list means "all channels"; normalising it keeps the stored
        # value and the dashboard unambiguous.
        self.notify_channels = normalized or [ALL_CHANNELS]
        self.headers = self._validate_headers(self.headers)
        self.extra_keywords = self._normalize_keywords(
            self.extra_keywords, "extra_keywords"
        )
        self.suppress_keywords = self._normalize_keywords(
            self.suppress_keywords, "suppress_keywords"
        )
        if self.auth is not None:
            if not isinstance(self.auth, str) or self.auth.count(":") != 1:
                raise ValueError("auth must be a 'user:password' string")
            user, password = self.auth.split(":", 1)
            if not user or not password:
                raise ValueError("auth must have a non-empty user and password")
        # ``bool`` is an ``int`` subclass but is not a meaningful count.
        if (
            isinstance(self.failure_count, bool)
            or not isinstance(self.failure_count, int)
            or self.failure_count < 0
        ):
            raise ValueError("failure_count must be a non-negative integer")
        if self.breaker_until is not None:
            if not isinstance(self.breaker_until, str):
                raise ValueError("breaker_until must be an ISO-8601 string or None")
            try:
                parse_iso8601(self.breaker_until)
            except ValueError:
                raise ValueError(
                    "breaker_until must be a parseable ISO-8601 string or None"
                )

    @staticmethod
    def _validate_headers(headers) -> Dict[str, str]:
        if headers is None:
            return {}
        if not isinstance(headers, dict):
            raise ValueError("headers must be an object of string values")
        normalized: Dict[str, str] = {}
        for name, value in headers.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("headers names must be non-empty strings")
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
                raise ValueError(
                    "headers names must not contain control characters or newlines"
                )
            if not isinstance(value, str):
                raise ValueError("headers values must be strings")
            if "\n" in value or "\r" in value:
                raise ValueError("headers values must not contain newlines")
            normalized[canonical_header_name(name)] = value
        return normalized

    @staticmethod
    def _normalize_keywords(terms, field_name: str) -> List[str]:
        """Normalize an override keyword list to trimmed lowercase terms.

        ``None`` is treated as an empty list. Non-list values and non-string
        (or blank) entries raise ``ValueError`` so callers cannot poison the
        relevance engine with malformed overrides.
        """
        if terms is None:
            return []
        if not isinstance(terms, (list, tuple)):
            raise ValueError("%s must be a list of strings" % field_name)
        normalized: List[str] = []
        for term in terms:
            if not isinstance(term, str) or not term.strip():
                raise ValueError("%s must be a list of strings" % field_name)
            term = term.strip().lower()
            if term not in normalized:
                normalized.append(term)
        return normalized

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "kind": self.kind,
            "api_name": self.api_name,
            "tracked_versions": list(self.tracked_versions),
            "enabled": self.enabled,
            "selector": self.selector,
            "min_severity": self.min_severity,
            "notify_min_severity": self.notify_min_severity,
            "notify_channels": list(self.notify_channels),
            "notify_webhook_url": self.notify_webhook_url,
            "scan_interval_seconds": self.scan_interval_seconds,
            "scan_jitter_seconds": self.scan_jitter_seconds,
            "max_bytes": self.max_bytes,
            "retention_days": self.retention_days,
            "alert_cooldown_seconds": self.alert_cooldown_seconds,
            "dedupe_window_seconds": self.dedupe_window_seconds,
            "headers": dict(self.headers),
            "auth": self.auth,
            "extra_keywords": list(self.extra_keywords),
            "suppress_keywords": list(self.suppress_keywords),
            "failure_count": self.failure_count,
            "breaker_until": self.breaker_until,
            "truncated": self.truncated,
            "created_at": self.created_at,
        }


@dataclass
class Entry:
    """A single parsed changelog/announcement item from a source."""

    source_id: int
    title: str
    body: str
    link: str = ""
    published: str = ""
    fingerprint: str = ""
    id: Optional[int] = None
    created_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "title": self.title,
            "body": self.body,
            "link": self.link,
            "published": self.published,
            "fingerprint": self.fingerprint,
            "created_at": self.created_at,
        }


@dataclass
class Alert:
    source_id: int
    entry_id: int
    severity: str
    reason: str
    matched_terms: List[str] = field(default_factory=list)
    version_hits: List[str] = field(default_factory=list)
    title: str = ""
    link: str = ""
    notified: bool = False
    digest_id: Optional[int] = None
    id: Optional[int] = None
    created_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_id": self.source_id,
            "entry_id": self.entry_id,
            "severity": self.severity,
            "reason": self.reason,
            "matched_terms": list(self.matched_terms),
            "version_hits": list(self.version_hits),
            "title": self.title,
            "link": self.link,
            "notified": self.notified,
            "digest_id": self.digest_id,
            "created_at": self.created_at,
        }


@dataclass
class Digest:
    """A grouped summary of pending alerts delivered on a slower cadence."""

    alert_count: int = 0
    severity: str = "info"
    body: str = ""
    subject: str = ""
    omitted: int = 0
    delivered: bool = False
    alert_ids: List[int] = field(default_factory=list)
    id: Optional[int] = None
    created_at: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "alert_count": self.alert_count,
            "severity": self.severity,
            "delivered": self.delivered,
            "subject": self.subject,
            "body": self.body,
            "omitted": self.omitted,
        }
