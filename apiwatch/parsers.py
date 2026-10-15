"""Parsers that normalize heterogeneous changelog formats.

Supported kinds:

* ``rss``        – RSS 2.0 and Atom XML feeds.
* ``json``       – JSON changelogs in several common shapes.
* ``html``       – HTML changelogs built from headings / list items.
* ``statuspage`` – statuspage.io JSON (``incidents`` / ``scheduled_maintenances``).
* ``github``     – GitHub releases JSON from the REST API (``/releases``).
* ``gitlab``     – GitLab releases JSON from the REST API (``/releases``).
* ``openapi``    – OpenAPI / Swagger specs, diffed between versions.

HTML and JSON sources accept an optional ``selector`` to narrow extraction:
for ``html`` the selector is a CSS-like simple selector (tag name, ``#id``,
``.class`` or a combination such as ``h2.deprecation``); for ``json``,
``github``, ``gitlab`` and ``openapi`` it is a dotted path such as
``data.releases`` or ``versions``.

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
    # Optional explicit severity for structured adapters (e.g. the OpenAPI diff
    # parser) that know the severity from the change itself. ``None`` means the
    # relevance engine classifies the entry from its text as usual.
    severity: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "body": self.body,
            "link": self.link,
            "published": self.published,
            "categories": list(self.categories),
            "severity": self.severity,
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
# GitHub releases
# --------------------------------------------------------------------------- #
def parse_github(text: str, base_url: str = "", selector: Optional[str] = None) -> List[ParsedEntry]:
    """Parse GitHub releases JSON (REST API ``/repos/:owner/:repo/releases``).

    Maps each release to a :class:`ParsedEntry`: title from ``name`` or
    ``tag_name``, body from ``body``, link from ``html_url``, date from
    ``published_at`` and categories from ``tag_name``, ``prerelease`` and
    ``draft``. Draft releases are skipped. Malformed input (including non-JSON)
    is tolerated and yields an empty list rather than raising.
    """
    if not text or not text.strip():
        return []
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []

    if selector:
        data = _apply_selector(data, selector)
    if not isinstance(data, list):
        return []

    entries: List[ParsedEntry] = []
    for item in data:
        if not isinstance(item, dict) or item.get("draft"):
            continue
        title = _first(item, ("name", "tag_name"))
        body = _first(item, ("body",))
        link = _first(item, ("html_url",))
        published = _first(item, ("published_at",))

        categories: List[str] = []
        tag = item.get("tag_name")
        if tag not in (None, ""):
            cleaned = _clean(str(tag))
            if cleaned:
                categories.append(cleaned)
        if item.get("prerelease"):
            categories.append("prerelease")
        if item.get("draft"):
            categories.append("draft")

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


# --------------------------------------------------------------------------- #
# GitLab releases
# --------------------------------------------------------------------------- #
def parse_gitlab(text: str, base_url: str = "", selector: Optional[str] = None) -> List[ParsedEntry]:
    """Parse GitLab releases JSON (REST API ``/projects/:id/releases``).

    Maps each release to a :class:`ParsedEntry`: title from ``name`` or
    ``tag_name``, body from ``description``, link from ``_links.self``, date
    from ``released_at`` and categories from ``tag_name`` and, when set,
    ``upcoming_release``. GitLab has no draft concept, so no release is skipped.
    Malformed input (including non-JSON) is tolerated and yields an empty list
    rather than raising.
    """
    if not text or not text.strip():
        return []
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return []

    if selector:
        data = _apply_selector(data, selector)
    if not isinstance(data, list):
        return []

    entries: List[ParsedEntry] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = _first(item, ("name", "tag_name"))
        body = _first(item, ("description",))
        links = item.get("_links")
        link = _first(links, ("self",)) if isinstance(links, dict) else ""
        published = _first(item, ("released_at",))

        categories: List[str] = []
        tag = item.get("tag_name")
        if tag not in (None, ""):
            cleaned = _clean(str(tag))
            if cleaned:
                categories.append(cleaned)
        if item.get("upcoming_release"):
            categories.append("upcoming_release")

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
# OpenAPI / Swagger diff
# --------------------------------------------------------------------------- #
# Severity assigned to each kind of structural change. Removing or narrowing a
# contract is critical, widening a contract is informational.
_OPENAPI_SEVERITY = {
    "removed path": "critical",
    "removed operation": "critical",
    "removed required parameter": "critical",
    "removed enum value": "critical",
    "changed parameter location": "critical",
    "changed response status code set": "critical",
    "added required parameter": "warning",
    "narrowed enum": "warning",
    "changed parameter type": "warning",
    "changed request schema type": "warning",
    "changed response schema type": "warning",
    "changed required property": "warning",
    "added path": "info",
    "added operation": "info",
    "added optional parameter": "info",
    "added enum value": "info",
    "added response status code": "info",
    "changed description": "info",
    "removed parameter": "info",
}

_OPENAPI_METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")


def _load_openapi_json(text: str):
    """Parse ``text`` as JSON, returning ``None`` on malformed input.

    Defensively unwraps a JSON string that itself contains a JSON document,
    which some hosts emit when they serialize a spec twice.
    """
    if not text or not text.strip():
        return None
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except (ValueError, TypeError):
            return None
    return data


def openapi_document(text: str):
    """Public wrapper used by the engine to spot malformed OpenAPI input."""
    return _load_openapi_json(text)


def _openapi_specs(data) -> List[dict]:
    """Normalize the selector result into a list of spec mappings."""
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        specs: List[dict] = []
        for item in data:
            if isinstance(item, dict):
                specs.append(item)
            elif isinstance(item, str):
                inner = _load_openapi_json(item)
                if isinstance(inner, dict):
                    specs.append(inner)
        return specs
    return []


def _openapi_type(value) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ",".join(str(part) for part in value)
    return str(value)


def _schema_type(schema) -> str:
    if not isinstance(schema, dict):
        return ""
    return _openapi_type(schema.get("type"))


def _fmt_values(values) -> str:
    if isinstance(values, (list, tuple, set)):
        return ", ".join(str(v) for v in values)
    return str(values) if values not in (None, "") else ""


def _normalize_text(value) -> str:
    if value in (None, ""):
        return ""
    return _WS_RE.sub(" ", str(value)).strip()


def _change(kind: str, *, method: str = "", path: str = "", old: str = "", new: str = "",
            detail: str = "") -> dict:
    return {
        "kind": kind,
        "severity": _OPENAPI_SEVERITY.get(kind, "info"),
        "method": method,
        "path": path,
        "old": old,
        "new": new,
        "detail": detail,
    }


def _param_list(operation: dict) -> List[dict]:
    raw = operation.get("parameters")
    if not isinstance(raw, list):
        return []
    return [
        param
        for param in raw
        if isinstance(param, dict) and param.get("name") not in (None, "")
    ]


def _param_schema(param: dict) -> dict:
    schema = param.get("schema")
    return schema if isinstance(schema, dict) else {}


def _param_type(param: dict) -> str:
    schema = _param_schema(param)
    if schema:
        return _schema_type(schema)
    return _openapi_type(param.get("type"))


def _param_enum(param: dict):
    schema = _param_schema(param)
    if isinstance(schema.get("enum"), list):
        return list(schema["enum"])
    if isinstance(param.get("enum"), list):
        return list(param["enum"])
    return None


def _diff_enum(kind_removed: str, change_kind: str, old_values, new_values,
               method: str, path: str, detail: str) -> List[dict]:
    changes: List[dict] = []
    if isinstance(old_values, list) and isinstance(new_values, list):
        removed = [v for v in old_values if v not in new_values]
        added = [v for v in new_values if v not in old_values]
        if removed:
            changes.append(_change(kind_removed, method=method, path=path,
                                   old=_fmt_values(old_values), new=_fmt_values(new_values),
                                   detail=detail))
        if added:
            changes.append(_change(change_kind, method=method, path=path,
                                   old=_fmt_values(old_values), new=_fmt_values(new_values),
                                   detail=detail))
    return changes


def _diff_parameter(old_param: dict, new_param: dict, method: str, path: str) -> List[dict]:
    changes: List[dict] = []
    name = str(new_param.get("name"))
    old_type = _param_type(old_param)
    new_type = _param_type(new_param)
    if old_type != new_type:
        changes.append(_change("changed parameter type", method=method, path=path,
                               old=old_type, new=new_type, detail="parameter %s" % name))
    changes.extend(_diff_enum("removed enum value", "added enum value",
                              _param_enum(old_param), _param_enum(new_param),
                              method, path, "parameter %s" % name))
    return changes


def _diff_parameters(old_op: dict, new_op: dict, method: str, path: str) -> List[dict]:
    changes: List[dict] = []
    old_params = _param_list(old_op)
    new_params = _param_list(new_op)
    old_by_key = {(str(p.get("name")), str(p.get("in", ""))): p for p in old_params}
    new_by_key = {(str(p.get("name")), str(p.get("in", ""))): p for p in new_params}
    old_by_name: Dict[str, List[dict]] = {}
    for param in old_params:
        old_by_name.setdefault(str(param.get("name")), []).append(param)
    new_by_name: Dict[str, List[dict]] = {}
    for param in new_params:
        new_by_name.setdefault(str(param.get("name")), []).append(param)

    moved = set()
    for name, olds in old_by_name.items():
        news = new_by_name.get(name)
        if not news or len(olds) != 1 or len(news) != 1:
            continue
        old_location = str(olds[0].get("in", ""))
        new_location = str(news[0].get("in", ""))
        if old_location != new_location:
            changes.append(_change(
                "changed parameter location", method=method, path=path,
                old="%s (%s)" % (name, old_location or "?"),
                new="%s (%s)" % (name, new_location or "?"),
            ))
            moved.add(name)

    for key, param in old_by_key.items():
        if key[0] in moved:
            continue
        if key in new_by_key:
            changes.extend(_diff_parameter(param, new_by_key[key], method, path))
        elif bool(param.get("required")):
            changes.append(_change("removed required parameter", method=method, path=path,
                                   old="required", new="absent", detail="%s (%s)" % key))
        else:
            changes.append(_change("removed parameter", method=method, path=path,
                                   old="present", new="absent", detail="%s (%s)" % key))

    for key, param in new_by_key.items():
        if key[0] in moved or key in old_by_key:
            continue
        if bool(param.get("required")):
            changes.append(_change("added required parameter", method=method, path=path,
                                   old="absent", new="required", detail="%s (%s)" % key))
        else:
            changes.append(_change("added optional parameter", method=method, path=path,
                                   old="absent", new="optional", detail="%s (%s)" % key))
    return changes


def _diff_schema(old_schema: dict, new_schema: dict, context: str, method: str,
                 path: str, pointer: str) -> List[dict]:
    changes: List[dict] = []
    old_type = _schema_type(old_schema)
    new_type = _schema_type(new_schema)
    if old_type != new_type:
        changes.append(_change("changed %s schema type" % context, method=method, path=path,
                               old=old_type, new=new_type, detail=pointer))
    changes.extend(_diff_enum("narrowed enum", "added enum value",
                              old_schema.get("enum"), new_schema.get("enum"),
                              method, path, pointer))
    old_required = {str(v) for v in old_schema.get("required", []) if v not in (None, "")}
    new_required = {str(v) for v in new_schema.get("required", []) if v not in (None, "")}
    if old_required != new_required:
        changes.append(_change("changed required property", method=method, path=path,
                               old=_fmt_values(sorted(old_required)),
                               new=_fmt_values(sorted(new_required)), detail=pointer))
    old_props = old_schema.get("properties")
    new_props = new_schema.get("properties")
    if isinstance(old_props, dict) and isinstance(new_props, dict):
        for name in sorted(set(old_props) & set(new_props)):
            old_prop = old_props.get(name)
            new_prop = new_props.get(name)
            if isinstance(old_prop, dict) and isinstance(new_prop, dict):
                changes.extend(_diff_schema(old_prop, new_prop, context, method, path,
                                            "%s.properties.%s" % (pointer, name)))
    return changes


def _request_schema(operation: dict):
    for param in _param_list(operation):
        if param.get("in") == "body" and isinstance(param.get("schema"), dict):
            return param["schema"]
    body = operation.get("requestBody")
    if isinstance(body, dict):
        content = body.get("content")
        if isinstance(content, dict):
            for media in content.values():
                if isinstance(media, dict) and isinstance(media.get("schema"), dict):
                    return media["schema"]
    return None


def _response_schema(response) -> dict:
    if not isinstance(response, dict):
        return {}
    schema = response.get("schema")
    if isinstance(schema, dict):
        return schema
    content = response.get("content")
    if isinstance(content, dict):
        for media in content.values():
            if isinstance(media, dict) and isinstance(media.get("schema"), dict):
                return media["schema"]
    return {}


def _diff_responses(old_op: dict, new_op: dict, method: str, path: str) -> List[dict]:
    changes: List[dict] = []
    old_responses = old_op.get("responses")
    new_responses = new_op.get("responses")
    old_responses = old_responses if isinstance(old_responses, dict) else {}
    new_responses = new_responses if isinstance(new_responses, dict) else {}
    for code in sorted(set(old_responses) - set(new_responses)):
        changes.append(_change("changed response status code set", method=method, path=path,
                               old=str(code), new="absent"))
    for code in sorted(set(new_responses) - set(old_responses)):
        changes.append(_change("added response status code", method=method, path=path,
                               old="absent", new=str(code)))
    for code in sorted(set(old_responses) & set(new_responses)):
        changes.extend(_diff_schema(_response_schema(old_responses[code]),
                                    _response_schema(new_responses[code]),
                                    "response", method, path, "response %s" % code))
    return changes


def _diff_operation(old_op: dict, new_op: dict, method: str, path: str) -> List[dict]:
    changes: List[dict] = []
    old_desc = _normalize_text(old_op.get("description") or old_op.get("summary"))
    new_desc = _normalize_text(new_op.get("description") or new_op.get("summary"))
    if old_desc != new_desc:
        changes.append(_change("changed description", method=method, path=path,
                               old=old_desc, new=new_desc))
    changes.extend(_diff_parameters(old_op, new_op, method, path))
    old_schema = _request_schema(old_op)
    new_schema = _request_schema(new_op)
    if isinstance(old_schema, dict) and isinstance(new_schema, dict):
        changes.extend(_diff_schema(old_schema, new_schema, "request", method, path, "requestBody"))
    changes.extend(_diff_responses(old_op, new_op, method, path))
    return changes


def _openapi_changes(old, new) -> List[dict]:
    """Diff two OpenAPI/Swagger specs into a list of structural changes.

    Pure and dependency-free so it can be unit-tested directly. Each change is a
    mapping with a ``kind``, a ``severity``, the affected ``method``/``path`` and
    the ``old``/``new`` values. Ordering-only changes and whitespace-only
    description changes are ignored so the content hash stays stable.
    """
    changes: List[dict] = []
    old_paths = old.get("paths") if isinstance(old, dict) else None
    new_paths = new.get("paths") if isinstance(new, dict) else None
    old_paths = old_paths if isinstance(old_paths, dict) else {}
    new_paths = new_paths if isinstance(new_paths, dict) else {}

    for path in sorted(set(old_paths) | set(new_paths)):
        in_old = path in old_paths
        in_new = path in new_paths
        if in_old and not in_new:
            changes.append(_change("removed path", path=path, old="present", new="absent"))
            continue
        if in_new and not in_old:
            changes.append(_change("added path", path=path, old="absent", new="present"))
            continue
        old_item = old_paths.get(path)
        new_item = new_paths.get(path)
        old_item = old_item if isinstance(old_item, dict) else {}
        new_item = new_item if isinstance(new_item, dict) else {}
        for method in _OPENAPI_METHODS:
            old_op = old_item.get(method)
            new_op = new_item.get(method)
            if isinstance(old_op, dict) and not isinstance(new_op, dict):
                changes.append(_change("removed operation", method=method.upper(), path=path,
                                       old="present", new="absent"))
            elif isinstance(new_op, dict) and not isinstance(old_op, dict):
                changes.append(_change("added operation", method=method.upper(), path=path,
                                       old="absent", new="present"))
            elif isinstance(old_op, dict) and isinstance(new_op, dict):
                changes.extend(_diff_operation(old_op, new_op, method.upper(), path))
    return changes


def _openapi_link(spec: dict, base_url: str) -> str:
    info = spec.get("info") if isinstance(spec, dict) else None
    contact = info.get("contact") if isinstance(info, dict) else None
    contact_url = contact.get("url") if isinstance(contact, dict) else None
    if contact_url:
        return urljoin(base_url, str(contact_url))
    return base_url or ""


def _openapi_version(spec: dict) -> str:
    info = spec.get("info") if isinstance(spec, dict) else None
    version = info.get("version") if isinstance(info, dict) else None
    return str(version) if version not in (None, "") else ""


def _openapi_entry(change: dict, link: str, published: str) -> ParsedEntry:
    prefix = " ".join(part for part in (change.get("method", ""), change.get("path", "")) if part)
    title = "%s: %s" % (prefix or "spec", change["kind"])
    body_parts = [
        "old: %s" % (change.get("old") or "(none)"),
        "new: %s" % (change.get("new") or "(none)"),
    ]
    if change.get("detail"):
        body_parts.append("at: %s" % change["detail"])
    categories = [change["kind"]]
    if change.get("path"):
        categories.append(change["path"])
    return ParsedEntry(
        title=title,
        body=" | ".join(body_parts),
        link=link,
        published=published,
        categories=categories,
        severity=change["severity"],
    )


def parse_openapi(text: str, base_url: str = "", selector: Optional[str] = None) -> List[ParsedEntry]:
    """Detect breaking-change signals by diffing OpenAPI/Swagger specs.

    The fetched document may be a single spec or a list of spec versions; the
    ``selector`` (dotted path) locates the list of versions, defaulting to the
    whole document treated as a single spec. A list is diffed pairwise (oldest
    first); a single spec is diffed against the empty spec so first-time
    registration surfaces the whole surface as ``info`` entries. Malformed or
    non-JSON input yields an empty list and never raises.
    """
    data = _load_openapi_json(text)
    if data is None:
        return []
    if selector:
        data = _apply_selector(data, selector)
    specs = _openapi_specs(data)
    if not specs:
        return []

    if len(specs) == 1:
        pairs = [({}, specs[0])]
    else:
        pairs = list(zip(specs, specs[1:]))

    entries: List[ParsedEntry] = []
    for old_spec, new_spec in pairs:
        link = _openapi_link(new_spec, base_url)
        published = _openapi_version(new_spec)
        for change in _openapi_changes(old_spec, new_spec):
            entries.append(_openapi_entry(change, link, published))
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
    if kind == "github":
        return parse_github(text, base_url, selector)
    if kind == "gitlab":
        return parse_gitlab(text, base_url, selector)
    if kind == "openapi":
        return parse_openapi(text, base_url, selector)
    return []
