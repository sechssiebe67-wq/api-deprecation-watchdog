"""Domain models for API Deprecation Watchdog.

Plain dataclasses so the core logic stays dependency-free and easy to test.
"""
from dataclasses import dataclass, field
from typing import List, Optional

SOURCE_KINDS = ("rss", "html", "json", "statuspage", "github", "openapi")
SEVERITIES = ("critical", "warning", "info")


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
    id: Optional[int] = None
    created_at: Optional[str] = None

    def __post_init__(self) -> None:
        if self.min_severity not in SEVERITIES:
            raise ValueError("min_severity must be one of %s" % ", ".join(SEVERITIES))

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
