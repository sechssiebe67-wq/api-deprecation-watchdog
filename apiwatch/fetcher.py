"""HTTP fetching abstraction.

The fetcher is the only component that touches the network. Tests inject a fake
implementation so the whole scanning pipeline can be exercised offline.

Every fetch returns a ``(text, truncated)`` tuple. The optional ``max_bytes``
argument caps how much of the response body is read and stored: at most that
many bytes are returned, and ``truncated`` reports whether the body was longer
than the cap. A runaway feed therefore cannot load a multi-megabyte body into
memory. When the response advertises a ``Content-Length`` that exceeds the cap
the fetch fails with :class:`FetchError` unless the caller explicitly opts in
with ``allow_truncated=True``, so a hostile feed is rejected rather than
silently clipped. A response without a ``Content-Length`` is read up to the cap
and reported as truncated when it is at least that long.
"""
import base64
from typing import Dict, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_USER_AGENT = "API-Deprecation-Watchdog/0.1 (+https://github.com/)"


class FetchError(Exception):
    pass


def _content_length(response) -> Optional[int]:
    """Return the declared ``Content-Length`` as an ``int``, or ``None``."""
    value = response.headers.get("Content-Length")
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class Fetcher:
    def __init__(self, timeout: int = 20, user_agent: str = DEFAULT_USER_AGENT) -> None:
        self.timeout = timeout
        self.user_agent = user_agent

    def fetch(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        auth: Optional[str] = None,
        max_bytes: Optional[int] = None,
        allow_truncated: bool = False,
    ) -> Tuple[str, bool]:
        request_headers = {"User-Agent": self.user_agent, "Accept": "*/*"}
        if headers:
            request_headers.update(headers)
        if auth:
            token = base64.b64encode(auth.encode("utf-8")).decode("ascii")
            request_headers["Authorization"] = "Basic " + token
        request = Request(url, headers=request_headers)
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 (validated upstream)
                charset = response.headers.get_content_charset() or "utf-8"
                declared = _content_length(response)
                if (
                    max_bytes is not None
                    and declared is not None
                    and declared > max_bytes
                    and not allow_truncated
                ):
                    raise FetchError(
                        "response Content-Length %d exceeds max_bytes %d for %s"
                        % (declared, max_bytes, url)
                    )
                if max_bytes is None:
                    data = response.read()
                    truncated = False
                else:
                    # Read one byte past the cap so an over-long body can be
                    # detected even without a trustworthy Content-Length; the
                    # extra byte is discarded and the returned text stays capped.
                    data = response.read(max_bytes + 1)
                    truncated = len(data) > max_bytes
                    if truncated:
                        data = data[:max_bytes]
                return data.decode(charset, "replace"), truncated
        except (HTTPError, URLError, ValueError, OSError) as exc:
            raise FetchError("failed to fetch %s: %s" % (url, exc)) from exc


class FakeFetcher(Fetcher):
    """Deterministic fetcher backed by an in-memory URL -> body mapping.

    Mirrors :class:`Fetcher`'s ``(text, truncated)`` contract and caps the body
    at ``max_bytes`` (utf-8 bytes) so engine tests can exercise truncation
    without any network access.
    """

    def __init__(self, responses: Optional[Dict[str, str]] = None) -> None:
        super().__init__(timeout=1)
        self.responses = dict(responses or {})
        self.calls = []
        self.call_headers = []
        self.call_auth = []
        self.call_max_bytes = []

    def fetch(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        auth: Optional[str] = None,
        max_bytes: Optional[int] = None,
        allow_truncated: bool = False,
    ) -> Tuple[str, bool]:
        self.calls.append(url)
        self.call_headers.append(dict(headers or {}))
        self.call_auth.append(auth)
        self.call_max_bytes.append(max_bytes)
        if url not in self.responses:
            raise FetchError("no fake response for %s" % url)
        value = self.responses[url]
        if isinstance(value, Exception):
            raise FetchError(str(value))
        if max_bytes is None:
            return value, False
        encoded = value.encode("utf-8")
        if len(encoded) <= max_bytes:
            return value, False
        return encoded[:max_bytes].decode("utf-8", "replace"), True
