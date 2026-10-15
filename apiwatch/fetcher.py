"""HTTP fetching abstraction.

The fetcher is the only component that touches the network. Tests inject a fake
implementation so the whole scanning pipeline can be exercised offline.
"""
from typing import Dict, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_USER_AGENT = "API-Deprecation-Watchdog/0.1 (+https://github.com/)"


class FetchError(Exception):
    pass


class Fetcher:
    def __init__(self, timeout: int = 20, user_agent: str = DEFAULT_USER_AGENT) -> None:
        self.timeout = timeout
        self.user_agent = user_agent

    def fetch(self, url: str) -> str:
        request = Request(url, headers={"User-Agent": self.user_agent, "Accept": "*/*"})
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 (validated upstream)
                charset = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(charset, "replace")
        except (HTTPError, URLError, ValueError, OSError) as exc:
            raise FetchError("failed to fetch %s: %s" % (url, exc)) from exc


class FakeFetcher(Fetcher):
    """Deterministic fetcher backed by an in-memory URL -> body mapping."""

    def __init__(self, responses: Optional[Dict[str, str]] = None) -> None:
        super().__init__(timeout=1)
        self.responses = dict(responses or {})
        self.calls = []

    def fetch(self, url: str) -> str:
        self.calls.append(url)
        if url not in self.responses:
            raise FetchError("no fake response for %s" % url)
        value = self.responses[url]
        if isinstance(value, Exception):
            raise FetchError(str(value))
        return value
