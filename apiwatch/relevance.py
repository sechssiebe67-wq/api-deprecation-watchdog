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


def _normalize_terms(terms) -> List[str]:
    """Return trimmed lowercase terms, dropping blanks and duplicates."""
    normalized: List[str] = []
    for term in terms or []:
        if not isinstance(term, str):
            continue
        term = term.strip().lower()
        if term and term not in normalized:
            normalized.append(term)
    return normalized


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


def analyze(
    entry: ParsedEntry,
    versions: List[str] = None,
    require_version: bool = False,
    extra_keywords: List[str] = None,
    suppress_keywords: List[str] = None,
) -> Relevance:
    """Classify an entry.

    ``require_version`` drops entries that do not mention any tracked version,
    which keeps the alert stream focused for teams with version-pinned usage.

    ``extra_keywords`` extends the built-in vocabulary per source: each term is
    matched case-insensitively on the same word boundaries as the built-in
    terms and raises an otherwise low-signal entry to ``critical``. A term in
    ``suppress_keywords`` is removed from the matched set before classification,
    so a source can mute a noisy phrase without losing the rest of the
    vocabulary. With both lists empty the result is identical to the legacy
    behaviour.
    """
    versions = versions or []
    extras = _normalize_terms(extra_keywords)
    suppressed = set(_normalize_terms(suppress_keywords))
    text = " ".join(
        [entry.title, entry.body] + list(entry.categories)
    )
    critical = _search_terms(
        text, tuple(dict.fromkeys(tuple(CRITICAL_TERMS) + tuple(extras)))
    )
    warning = _search_terms(text, WARNING_TERMS)
    info = _search_terms(text, INFO_TERMS)
    version_hits = _version_hits(text, versions)

    if suppressed:
        critical = [term for term in critical if term not in suppressed]
        warning = [term for term in warning if term not in suppressed]
        info = [term for term in info if term not in suppressed]

    matched = critical + warning + info

    if require_version and versions and not version_hits:
        return Relevance(relevant=False, matched_terms=matched, version_hits=[])

    # Structured adapters (e.g. the OpenAPI diff parser) know the severity from
    # the change itself, so trust that explicit hint over keyword matching.
    declared = getattr(entry, "severity", None)
    if declared in _SEVERITY_RANK:
        reason_parts = ["change: " + declared]
        if matched:
            reason_parts.append("matched: " + ", ".join(matched[:5]))
        if version_hits:
            reason_parts.append("versions: " + ", ".join(version_hits))
        return Relevance(
            relevant=True,
            severity=declared,
            reason="; ".join(reason_parts),
            matched_terms=matched,
            version_hits=version_hits,
        )

    if critical:
        severity = "critical"
    elif warning:
        severity = "warning"
    elif info or version_hits:
        severity = "info"
    else:
        return Relevance(relevant=False, matched_terms=matched, version_hits=version_hits)

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
