"""Parsers that normalize heterogeneous changelog formats.

Supported kinds:

* ``rss``        – RSS 2.0 and Atom XML feeds.
* ``json``       – JSON changelogs in several common shapes.
* ``html``       – HTML changelogs built from headings / list items.
* ``statuspage`` – statuspage.io JSON (``incidents`` / ``scheduled_maintenances``).

HTML and JSON sources accept an optional ``selector`` to narrow extraction:
for ``html`` the selector is a CSS-like simple selector (tag name, ``#id``,
``.class`` or a combination such as ``h2.deprecation``); for ``json`` it is a
dotted path such as ``data.releases``.

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
# statuspage.io
# --------------------------------------------------------------------------- #
def parse_statuspage(text: str, base_url: str = "", selector: Optional[str] = None) -> List[ParsedEntry]:
    """Parse a statuspage.io JSON document.

    Reads the ``incidents`` and ``scheduled_maintenances`` arrays and maps each
    element to a :class:`ParsedEntry`. Malformed input (including non-JSON) is
    tolerated and yields an empty list rather than raising.
    """
    if not text or not text.strip():
        return []
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []
    try:
        if selector:
            data = _apply_selector(data, selector)
        if not isinstance(data, dict):
            return []
        entries: List[ParsedEntry] = []
        for key in ("incidents", "scheduled_maintenances"):
            items = data.get(key)
            if not isinstance(items, list):
                continue
            for item in items:
                if not isinstance(item, dict):
                    continue
                entry = _statuspage_entry(item, base_url)
                if entry.title or entry.body:
                    entries.append(entry)
        return entries
    except Exception:  # defensive: malformed payloads must never crash a scan
        return []


def _statuspage_entry(item: dict, base_url: str) -> ParsedEntry:
    title = _first(item, ("name", "title"))
    published = _first(item, ("created_at", "updated_at"))
    link = _first(item, ("shortlink", "url"))
    body = ""
    updates = item.get("incident_updates")
    if isinstance(updates, list):
        parts: List[str] = []
        for update in updates:
            if isinstance(update, dict):
                part = _first(update, ("body", "text", "update"))
            else:
                part = _clean(str(update))
            if part:
                parts.append(part)
        body = " ".join(parts)
    if not body:
        body = _first(item, ("body",))

    categories: List[str] = []
    for key in ("status", "impact"):
        value = item.get(key)
        if value not in (None, ""):
            cleaned = _clean(str(value))
            if cleaned:
                categories.append(cleaned)

    return ParsedEntry(
        title=title or body[:120],
        body=body,
        link=urljoin(base_url, link) if link else "",
        published=published,
        categories=categories,
    )


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
_HTML_BLOCK_TAGS = frozenset({"h1", "h2", "h3", "h4", "h5", "h6", "p", "li"})
_VOID_TAGS = frozenset(
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
)
# Simple CSS selector: optional tag, optional #id, optional one or more .class
_CSS_SELECTOR_RE = re.compile(
    r"^(?P<tag>[a-zA-Z][a-zA-Z0-9]*)?(?P<id>#[\w-]+)?(?P<classes>(?:\.[\w-]+)*)$"
)


class _HtmlCollector(HTMLParser):
    """Collects headings, paragraphs and list items in document order.

    Each collected block remembers the chain of open elements (its ``path``) so
    a selector can scope by ancestor id/class as well as by the block's own tag
    and attributes.
    """

    _BLOCK_TAGS = _HTML_BLOCK_TAGS

    def __init__(self) -> None:
        HTMLParser.__init__(self, convert_charrefs=True)
        self.blocks: List[Dict[str, object]] = []
        self.links: List[str] = []
        self._stack: List[Dict[str, object]] = []
        self._tag: Optional[str] = None
        self._buf: List[str] = []
        self._path: List[Dict[str, object]] = []

    def _element(self, tag: str, attrs) -> Dict[str, object]:
        element: Dict[str, object] = {"tag": tag, "id": "", "classes": set()}
        for key, value in attrs:
            if key == "id" and value:
                element["id"] = value
            elif key == "class" and value:
                element["classes"] = set(value.split())
        return element

    def handle_starttag(self, tag: str, attrs) -> None:
        element = self._element(tag, attrs)
        if tag not in _VOID_TAGS:
            self._stack.append(element)
        if tag in self._BLOCK_TAGS:
            self._flush()
            self._tag = tag
            self._path = list(self._stack)
        elif tag == "a":
            for key, value in attrs:
                if key == "href" and value:
                    self.links.append(value)

    def handle_startendtag(self, tag: str, attrs) -> None:
        if tag == "a":
            for key, value in attrs:
                if key == "href" and value:
                    self.links.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag in self._BLOCK_TAGS:
            self._flush()
        for index in range(len(self._stack) - 1, -1, -1):
            if self._stack[index]["tag"] == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._tag and data.strip():
            self._buf.append(data)

    def _flush(self) -> None:
        if self._tag and self._buf:
            text = _clean(" ".join(self._buf))
            if text:
                self.blocks.append({"tag": self._tag, "text": text, "path": list(self._path)})
        self._tag = None
        self._buf = []
        self._path = []

    def close(self) -> None:
        try:
            HTMLParser.close(self)
        finally:
            self._flush()


def _parse_css_selector(selector: str):
    match = _CSS_SELECTOR_RE.match(selector.strip())
    if not match:
        return None
    tag = (match.group("tag") or "").lower()
    id_token = match.group("id")
    id_name = id_token[1:] if id_token else ""
    classes = [part for part in (match.group("classes") or "").split(".") if part]
    if not (tag or id_name or classes):
        return None
    return tag, id_name, classes


def _is_html_selector(selector: str) -> bool:
    """True when the selector is structural (tag/#id/.class) rather than text."""
    stripped = selector.strip()
    if not stripped:
        return False
    if "#" in stripped or "." in stripped:
        return True
    return stripped.lower() in _HTML_BLOCK_TAGS


def _element_matches(element: Dict[str, object], tag: str, id_name: str, classes) -> bool:
    if tag and element.get("tag") != tag:
        return False
    if id_name and element.get("id") != id_name:
        return False
    element_classes = element.get("classes") or set()
    for cls in classes:
        if cls not in element_classes:
            return False
    return True


def _block_matches(block: Dict[str, object], parsed) -> bool:
    tag, id_name, classes = parsed
    return any(
        _element_matches(element, tag, id_name, classes)
        for element in block.get("path", [])  # type: ignore[union-attr]
    )


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
        parsed = _parse_css_selector(selector) if _is_html_selector(selector) else None
        if parsed is not None:
            blocks = [block for block in blocks if _block_matches(block, parsed)]
        else:
            blocks = [block for block in blocks if selector.lower() in block["text"].lower()]

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
    if kind == "statuspage":
        return parse_statuspage(text, base_url, selector)
    return []
