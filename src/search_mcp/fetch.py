"""fetch_url: SSRF-guarded fetch + clean text extraction.

Hardening (pre-launch security audit, 2026-10-01):
- H-1: redirects are walked MANUALLY (follow_redirects=False, max 5 hops).
  Every hop's Location is resolved and validated BEFORE it is followed, so
  intermediate 301/302/307/308 hops can never smuggle a request to an
  internal host past the guard.
- H-2: DNS is resolved exactly ONCE per hop, inside _validate_url(). The TCP
  connection then dials that validated IP literal via a pinned httpcore
  network backend (_PinnedIPBackend) instead of letting httpcore re-resolve
  the hostname at connect time (the DNS-rebinding TOCTOU). The request URL
  keeps the original hostname, so the Host header and TLS SNI are unchanged.
  trust_env=False on the fetch client: an ambient HTTP(S)_PROXY would do its
  own DNS and silently bypass the pin.
- H-3: bodies stream (httpx.stream + iter_bytes) with a running byte cap.
  Content-Length is pre-checked, a shared per-fetch deadline bounds
  slow-drip connections, and the cap applies to the DECODED body, so gzip
  bombs are caught too.

Every resolved IP (A/AAAA) of the target host must be a public unicast
address: private, loopback, link-local, multicast, reserved, unspecified,
and CGNAT (100.64.0.0/10) ranges are rejected via `not ip.is_global`,
which also blocks the cloud metadata endpoint (169.254.169.254).
"""

from __future__ import annotations

import ipaddress
import socket
import time
import urllib.parse
from html.parser import HTMLParser
from typing import NamedTuple

import httpcore
import httpx

MAX_URL_BYTES = 5 * 1024 * 1024  # 5 MB cap on fetched pages
MAX_REDIRECT_HOPS = 5  # H-1: manual redirect walk, validated per hop
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
USER_AGENT = "search-mcp/0.1.0 (+https://github.com/Anon2052/search-mcp)"


class FetchError(Exception):
    """Raised when a URL cannot be fetched or its text extracted."""


class _FetchResult(NamedTuple):
    final_url: str
    headers: dict
    status_code: int
    body: bytes
    encoding: str | None


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


def _validate_url(url: str) -> tuple[urllib.parse.ParseResult, str]:
    """Parse a URL, resolve its host ONCE, and reject anything non-public.

    Returns ``(parsed_parts, pinned_ip)``: the single validated IP literal
    the TCP connection must dial. Every resolved A/AAAA record must be a
    public unicast address.
    """
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
    pinned: str | None = None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        # `not is_global` covers private, loopback, link-local, multicast,
        # reserved, unspecified — AND carrier-grade NAT (100.64.0.0/10),
        # which the individual is_* flags miss on some Python builds.
        if not ip.is_global:
            raise FetchError(f"URL: host '{host}' resolves to a non-public IP ({ip})")
        if pinned is None:
            pinned = info[4][0]
    if pinned is None:
        raise FetchError(f"URL: host '{host}' did not resolve to any address")
    return parts, pinned


class _PinnedIPBackend(httpcore.NetworkBackend):
    """httpcore network backend that dials a pre-validated IP literal (H-2).

    httpcore resolves the URL host itself at connect time — the
    DNS-rebinding race. This backend substitutes the IP that _validate_url()
    already resolved and vetted. The request URL is untouched, so TLS SNI
    and the Host header still use the original hostname.
    """

    def __init__(self, pinned_ip: str) -> None:
        self._pinned_ip = pinned_ip
        self._inner = httpcore.SyncBackend()

    def connect_tcp(  # noqa: ANN001, ANN202
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        # `host` is intentionally ignored: the socket must dial the vetted IP.
        return self._inner.connect_tcp(
            self._pinned_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    def connect_unix_socket(self, path, timeout=None, socket_options=None):  # noqa: ANN001, ANN202
        raise httpcore.ConnectError("unix sockets are not supported by the fetch guard")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


class _PinnedIPTransport(httpx.HTTPTransport):
    """httpx transport pinning every TCP dial to one validated IP (H-2)."""

    def __init__(self, pinned_ip: str) -> None:
        # Build the pool directly (mirroring httpx.HTTPTransport defaults)
        # with the pinned backend. super().__init__() is deliberately NOT
        # called: it would build an unpinned pool we immediately discard.
        # Coupled to pinned httpx==0.28.1 / httpcore==1.0.9 (see
        # requirements.txt); handle_request/close only touch self._pool.
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(verify=True, cert=None, trust_env=False),
            network_backend=_PinnedIPBackend(pinned_ip),
        )


def _read_capped(resp: httpx.Response, max_bytes: int, deadline: float) -> bytes:
    """Stream the (decoded) response body with a running byte cap (H-3)."""
    declared = resp.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                raise FetchError(
                    f"URL: content-length {declared} exceeds {max_bytes} byte limit"
                )
        except ValueError:
            pass  # malformed header; the streaming cap below still applies
    chunks: list[bytes] = []
    total = 0
    # iter_bytes() yields DECODED bytes (gzip/deflate already inflated), so
    # the running total caps decompression bombs, not just wire bytes.
    for chunk in resp.iter_bytes(chunk_size=65536):
        total += len(chunk)
        if total > max_bytes:
            raise FetchError(f"URL: response body exceeds {max_bytes} byte limit")
        if time.monotonic() > deadline:
            raise FetchError("URL: fetch timed out")
        chunks.append(chunk)
    return b"".join(chunks)


def _fetch_guarded(url: str, timeout: int = 20) -> _FetchResult:
    """Fetch with per-hop redirect validation (H-1), DNS pinning (H-2),
    and a streaming body cap (H-3).
    """
    deadline = time.monotonic() + timeout
    current = url
    for _hop in range(MAX_REDIRECT_HOPS + 1):
        # Resolve + validate THIS hop before any socket is opened for it.
        _parts, pinned_ip = _validate_url(current)
        hop_timeout = max(1.0, deadline - time.monotonic())
        transport = _PinnedIPTransport(pinned_ip)
        try:
            with httpx.Client(
                transport=transport,
                timeout=hop_timeout,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                with client.stream("GET", current, follow_redirects=False) as resp:
                    if resp.status_code in _REDIRECT_STATUSES:
                        location = resp.headers.get("location")
                        if not location:
                            raise FetchError("URL: redirect without Location header")
                        # Next hop is validated at the top of the loop.
                        current = urllib.parse.urljoin(current, location)
                        continue
                    if resp.status_code >= 400:
                        return _FetchResult(
                            str(resp.url), dict(resp.headers), resp.status_code, b"", None
                        )
                    body = _read_capped(resp, MAX_URL_BYTES, deadline)
                    return _FetchResult(
                        str(resp.url),
                        dict(resp.headers),
                        resp.status_code,
                        body,
                        resp.encoding,
                    )
        except httpx.HTTPError as exc:
            raise FetchError(f"URL: fetch failed ({exc})") from exc
    raise FetchError(f"URL: too many redirects (>{MAX_REDIRECT_HOPS} hops)")


def fetch_url(url: str, timeout: int = 20) -> str:
    """Fetch a web page and return its main text content, stripped of markup."""
    result = _fetch_guarded(url, timeout)
    content_type = result.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type:
        raise FetchError(f"URL: unsupported content type '{content_type}'")
    if result.status_code >= 400:
        raise FetchError(f"URL: HTTP {result.status_code} for {url}")
    try:
        html_text = result.body.decode(result.encoding or "utf-8", errors="replace")
    except (LookupError, UnicodeDecodeError) as exc:
        raise FetchError(f"URL: undecodable response ({exc})") from exc
    extractor = _TextExtractor()
    extractor.feed(html_text)
    text = extractor.text()
    if not text:
        raise FetchError("URL: no extractable text found")
    return text
