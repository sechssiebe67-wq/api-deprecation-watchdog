"""Parsers that normalize heterogeneous changelog formats.

Supported kinds:

* ``rss``  – RSS 2.0 and Atom XML feeds.
* ``json`` – JSON changelogs in several common shapes.
* ``html`` – HTML changelogs built from headings / list items.

All parsers return a list of :class:`ParsedEntry` objects and never raise on
malformed input; they degrade to an empty list instead. Pure functions keep the
core testable without network access.
"""
import hashlib
import html as html_module
import json
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Dict, List, Optional
from urllib.parse import urljoin
from xml.etree import ElementTree

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")

_TITLE_KEYS = ("title", "name", "headline", "subject", "version")
_BODY_KEYS = (
    "body",
    "description",
    "summary",
    "content",
    "details",
    "text",
    "note",
    "notes",
    "changes",
)
_LINK_KEYS = ("link", "url", "html_url", "permalink", "href")
_DATE_KEYS = (
    "published",
    "pubDate",
    "date",
    "created_at",
    "createdAt",
    "updated_at",
    "updatedAt",
    "updated",
    "release_date",
)
_CONTAINER_KEYS = ("items", "entries", "releases", "changes", "data", "results", "posts")


def _clean(text: Optional[str]) -> str:
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = html_module.unescape(text)
    return _WS_RE.sub(" ", text).strip()


def fingerprint(*parts: str) -> str:
    raw = "\n".join(part or "" for part in parts)
    return hashlib.sha256(raw.encode("utf-8", "replace")).hexdigest()


@dataclass
class ParsedEntry:
    title: str
    body: str = ""
    link: str = ""
    published: str = ""
    categories: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "body": self.body,
            "link": self.link,
            "published": self.published,
            "categories": list(self.categories),
        }


# --------------------------------------------------------------------------- #
# RSS / Atom
# --------------------------------------------------------------------------- #
def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_rss(text: str, base_url: str = "") -> List[ParsedEntry]:
    if not text or not text.strip():
        return []
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return []

    entries: List[ParsedEntry] = []
    for node in root.iter():
        if _localname(node.tag) not in ("item", "entry"):
            continue
        entry = _rss_entry(node, base_url)
        if entry.title or entry.body:
            entries.append(entry)
    return entries


def _rss_entry(node, base_url: str) -> ParsedEntry:
    title = body = published = link = ""
    categories: List[str] = []
    for child in node:
        name = _localname(child.tag)
        if name == "title":
            title = _clean(child.text)
        elif name in ("description", "summary"):
            if not body:
                body = _clean(child.text)
        elif name in ("encoded", "content"):
            body = _clean(child.text)
        elif name == "link":
            href = child.attrib.get("href")
            link = href or _clean(child.text)
        elif name in ("pubdate", "published", "updated"):
            published = _clean(child.text)
        elif name == "category":
            value = child.attrib.get("term") or _clean(child.text)
            if value:
                categories.append(value)
        elif name == "id" and not link:
            link = _clean(child.text)
    return ParsedEntry(
        title=title or _clean(body)[:120],
        body=body,
        link=urljoin(base_url, link) if link else "",
        published=published,
        categories=categories,
    )


# --------------------------------------------------------------------------- #
# JSON
# --------------------------------------------------------------------------- #
def _first(mapping: dict, keys) -> str:
    for key in keys:
        if key in mapping and mapping[key] not in (None, ""):
            value = mapping[key]
            if isinstance(value, (dict, list)):
                nested = _flatten(value)
                if nested:
                    return nested
            else:
                return _clean(str(value))
    return ""


def _flatten(value) -> str:
    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, dict):
        # prefer common content wrappers, else concatenate scalar values
        for key in ("rendered", "text", "body", "value", "markdown", "html"):
            if key in value:
                return _flatten(value[key])
        return " ".join(_flatten(v) for v in value.values() if isinstance(v, (str, dict, list)))
    if isinstance(value, list):
        return " ".join(_flatten(v) for v in value)
    return _clean(str(value))


def _find_items(data) -> List[dict]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in _CONTAINER_KEYS:
            if isinstance(data.get(key), list):
                return [item for item in data[key] if isinstance(item, dict)]
        # single object
        if any(key in data for key in _TITLE_KEYS):
            return [data]
    return []


def parse_json(text: str, base_url: str = "", selector: Optional[str] = None) -> List[ParsedEntry]:
    if not text or not text.strip():
        return []
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []

    if selector:
        data = _apply_selector(data, selector)

    entries: List[ParsedEntry] = []
    for item in _find_items(data):
        title = _first(item, _TITLE_KEYS)
        body = _first(item, _BODY_KEYS)
        link = _first(item, _LINK_KEYS)
        published = _first(item, _DATE_KEYS)
        categories = _extract_categories(item)
        if not (title or body):
            continue
        entries.append(
            ParsedEntry(
                title=title or body[:120],
                body=body,
                link=urljoin(base_url, link) if link else "",
                published=published,
                categories=categories,
            )
        )
    return entries


def _extract_categories(item: dict) -> List[str]:
    result: List[str] = []
    for key in ("categories", "tags", "labels"):
        value = item.get(key)
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, str):
                    result.append(entry)
                elif isinstance(entry, dict):
                    name = entry.get("name") or entry.get("term")
                    if name:
                        result.append(str(name))
    for key in ("category", "tag", "label"):
        value = item.get(key)
        if isinstance(value, str) and value:
            result.append(value)
    return result


def _apply_selector(data, selector: str):
    """Very small dotted-path selector, e.g. ``data.releases``."""
    current = data
    for part in selector.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return []
    return current if current is not None else []


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
class _HtmlCollector(HTMLParser):
    """Collects headings, paragraphs and list items in document order."""

    _BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li"}

    def __init__(self) -> None:
        HTMLParser.__init__(self, convert_charrefs=True)
        self.blocks: List[Dict[str, str]] = []
        self._tag: Optional[str] = None
        self._buf: List[str] = []
        self._current_link: Optional[str] = None
        self.links: List[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in self._BLOCK_TAGS:
            self._flush()
            self._tag = tag
        elif tag == "a":
            for key, value in attrs:
                if key == "href" and value:
                    self._current_link = value
                    self.links.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK_TAGS:
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._tag and data.strip():
            self._buf.append(data)

    def _flush(self) -> None:
        if self._tag and self._buf:
            text = _clean(" ".join(self._buf))
            if text:
                self.blocks.append({"tag": self._tag, "text": text})
        self._tag = None
        self._buf = []

    def close(self) -> None:
        try:
            HTMLParser.close(self)
        finally:
            self._flush()


def parse_html(text: str, base_url: str = "", selector: Optional[str] = None) -> List[ParsedEntry]:
    if not text or not text.strip():
        return []
    collector = _HtmlCollector()
    try:
        collector.feed(text)
        collector.close()
    except Exception:  # defensive: malformed markup should never crash a scan
        return []

    blocks = collector.blocks
    if selector:
        blocks = [b for b in blocks if selector.lower() in b["text"].lower()]

    entries: List[ParsedEntry] = []
    current: Optional[ParsedEntry] = None
    for block in blocks:
        if block["tag"].startswith("h"):
            if current:
                entries.append(current)
            current = ParsedEntry(title=block["text"])
        elif current is not None:
            current.body = (current.body + " " + block["text"]).strip()
        else:
            entries.append(ParsedEntry(title=block["text"], body=block["text"]))
    if current:
        entries.append(current)

    for entry in entries:
        if not entry.body:
            entry.body = entry.title
    return entries


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #
def parse(kind: str, text: str, base_url: str = "", selector: Optional[str] = None) -> List[ParsedEntry]:
    kind = (kind or "").lower()
    if kind == "rss":
        return parse_rss(text, base_url)
    if kind == "json":
        return parse_json(text, base_url, selector)
    if kind == "html":
        return parse_html(text, base_url, selector)
    return []
