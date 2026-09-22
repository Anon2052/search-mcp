"""fetch_url: SSRF-guarded fetch + clean text extraction.

Same guard pattern as doc-convert-mcp's url_to_clean_text: every resolved IP
(A/AAAA) of the target host — and of the final host after redirects — must be
a public unicast address. Private, loopback, link-local, multicast, reserved,
and unspecified ranges are rejected, which also blocks the cloud metadata
endpoint (169.254.169.254).
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse
from html.parser import HTMLParser

import httpx

MAX_URL_BYTES = 5 * 1024 * 1024  # 5 MB cap on fetched pages
USER_AGENT = "search-mcp/0.1.0 (+https://github.com/Anon2052/search-mcp)"


class FetchError(Exception):
    """Raised when a URL cannot be fetched or its text extracted."""


class _TextExtractor(HTMLParser):
    """Minimal HTML -> text: drops script/style, keeps block structure."""

    BLOCK_TAGS = {
        "p",
        "div",
        "br",
        "li",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "tr",
        "section",
        "article",
        "header",
        "footer",
    }

    def __init__(self) -> None:
        super().__init__()
        self._parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip += 1
        elif tag in self.BLOCK_TAGS and not self._skip:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript"):
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK_TAGS and not self._skip:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self._parts.append(data)

    def text(self) -> str:
        lines = (line.strip() for line in "".join(self._parts).splitlines())
        return "\n".join(line for line in lines if line)


def _validate_url(url: str) -> urllib.parse.ParseResult:
    """Parse a URL and reject anything that smells like SSRF."""
    try:
        parts = urllib.parse.urlparse(url)
    except ValueError as exc:
        raise FetchError(f"URL: malformed ({exc})") from exc
    if parts.scheme not in ("http", "https"):
        raise FetchError("URL: only http(s) URLs are allowed")
    host = parts.hostname or ""
    if not host:
        raise FetchError("URL: missing hostname")
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise FetchError(f"URL: cannot resolve host '{host}' ({exc})") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise FetchError(f"URL: host '{host}' resolves to a non-public IP ({ip})")
    return parts


def fetch_url(url: str, timeout: int = 20) -> str:
    """Fetch a web page and return its main text content, stripped of markup."""
    _validate_url(url)
    try:
        resp = httpx.get(
            url,
            timeout=timeout,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
        )
    except httpx.HTTPError as exc:
        raise FetchError(f"URL: fetch failed ({exc})") from exc
    # Re-validate after redirects (httpx followed them already; check final host).
    _validate_url(str(resp.url))
    content_type = resp.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type:
        raise FetchError(f"URL: unsupported content type '{content_type}'")
    if resp.status_code >= 400:
        raise FetchError(f"URL: HTTP {resp.status_code} for {url}")
    body = resp.content[:MAX_URL_BYTES]
    try:
        html_text = body.decode(resp.encoding or "utf-8", errors="replace")
    except (LookupError, UnicodeDecodeError) as exc:
        raise FetchError(f"URL: undecodable response ({exc})") from exc
    extractor = _TextExtractor()
    extractor.feed(html_text)
    text = extractor.text()
    if not text:
        raise FetchError("URL: no extractable text found")
    return text
