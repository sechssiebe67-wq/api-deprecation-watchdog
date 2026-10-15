"""Relevance engine.

Turns a raw :class:`~apiwatch.parsers.ParsedEntry` into an alert decision by
looking for deprecation / breaking-change language and checking whether the
entry references a version the team actually tracks.
"""
import re
from dataclasses import dataclass, field
from typing import List

from .parsers import ParsedEntry

# Terms are grouped by severity. Matching is case-insensitive on word
# boundaries so "deprecated" does not match inside "undeprecated" etc.
CRITICAL_TERMS = (
    "breaking change",
    "breaking changes",
    "breaking",
    "no longer supported",
    "no longer available",
    "will be removed",
    "has been removed",
    "shutting down",
    "shutdown",
    "sunset",
    "end of life",
    "eol",
    "retirement",
    "retired",
    "decommission",
)
WARNING_TERMS = (
    "deprecated",
    "deprecation",
    "deprecate",
    "legacy",
    "migration",
    "migrate",
    "upgrade required",
    "action required",
    "discontinued",
    "unsupported",
)
INFO_TERMS = (
    "notice",
    "announcement",
    "new version",
    "release",
)

_SEVERITY_RANK = {"critical": 3, "warning": 2, "info": 1}


@dataclass
class Relevance:
    relevant: bool
    severity: str = "info"
    reason: str = ""
    matched_terms: List[str] = field(default_factory=list)
    version_hits: List[str] = field(default_factory=list)


def _search_terms(text: str, terms) -> List[str]:
    hits: List[str] = []
    lowered = text.lower()
    for term in terms:
        if " " in term:
            if term in lowered:
                hits.append(term)
        elif re.search(r"\b" + re.escape(term) + r"\b", lowered):
            hits.append(term)
    return hits


def _version_hits(text: str, versions: List[str]) -> List[str]:
    lowered = text.lower()
    hits: List[str] = []
    for version in versions or []:
        if not version:
            continue
        token = version.lower()
        if " " in token:
            if token in lowered:
                hits.append(version)
        elif re.search(r"(?<![a-z0-9])" + re.escape(token) + r"(?![a-z0-9])", lowered):
            hits.append(version)
    return hits


def analyze(entry: ParsedEntry, versions: List[str] = None, require_version: bool = False) -> Relevance:
    """Classify an entry.

    ``require_version`` drops entries that do not mention any tracked version,
    which keeps the alert stream focused for teams with version-pinned usage.
    """
    versions = versions or []
    text = " ".join(
        [entry.title, entry.body] + list(entry.categories)
    )
    critical = _search_terms(text, CRITICAL_TERMS)
    warning = _search_terms(text, WARNING_TERMS)
    info = _search_terms(text, INFO_TERMS)
    version_hits = _version_hits(text, versions)

    matched = critical + warning + info
    if critical:
        severity = "critical"
    elif warning:
        severity = "warning"
    elif info or version_hits:
        severity = "info"
    else:
        return Relevance(relevant=False, matched_terms=matched, version_hits=version_hits)

    if require_version and versions and not version_hits:
        return Relevance(relevant=False, matched_terms=matched, version_hits=[])

    reason_parts = []
    if matched:
        reason_parts.append("matched: " + ", ".join(matched[:5]))
    if version_hits:
        reason_parts.append("versions: " + ", ".join(version_hits))
    if not reason_parts:
        reason_parts.append("potential relevance")

    return Relevance(
        relevant=True,
        severity=severity,
        reason="; ".join(reason_parts),
        matched_terms=matched,
        version_hits=version_hits,
    )


def severity_rank(severity: str) -> int:
    """Rank a severity on the ``critical`` > ``warning`` > ``info`` ladder."""
    return _SEVERITY_RANK.get(severity, 0)


def meets_floor(severity: str, floor: str) -> bool:
    """Return whether ``severity`` ranks at or above the ``floor``."""
    return severity_rank(severity) >= severity_rank(floor)


def max_severity(severities: List[str]) -> str:
    if not severities:
        return "info"
    return max(severities, key=lambda s: _SEVERITY_RANK.get(s, 0))
