"""Tests for fetch_url: SSRF guard, redirect walk, DNS pinning, body caps."""

from __future__ import annotations

import ipaddress
import socket
from types import SimpleNamespace

import httpx
import pytest

from search_mcp import fetch as fetch_mod
from search_mcp.fetch import FetchError, fetch_url

DEFAULT_BODY = (
    b"<html><head><title>T</title></head><body>"
    b"<script>evil()</script><style>.x{}</style>"
    b"<h1>Headline</h1><p>Body text here.</p></body></html>"
)


def _addrinfo(ip):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))]


@pytest.fixture()
def fake_dns(monkeypatch):
    """DNS: example.com -> public IP; IP literals resolve to themselves."""
    mapping = {"example.com": "93.184.216.34"}

    def getaddrinfo(host, port, *args, **kwargs):
        try:
            ipaddress.ip_address(host)
            return _addrinfo(host)
        except ValueError:
            pass
        ip = mapping.get(host)
        if ip is None:
            raise socket.gaierror("no such host")
        return _addrinfo(ip)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return mapping


@pytest.fixture()
def fake_http(monkeypatch):
    """Replace httpx.Client with a fake whose .stream() serves canned hops.

    Returns a namespace with ``calls`` (requested URLs, in order) and
    ``routes`` (url -> (status, headers, chunks)). The fake asserts
    follow_redirects is never True (H-1) and that a transport is always
    supplied (H-2 pinning).
    """

    class FakeResp:
        def __init__(self, url, status, headers, chunks):
            self.url = url
            self.status_code = status
            self.headers = headers
            self._chunks = chunks
            self.encoding = "utf-8"

        def iter_bytes(self, chunk_size=None):
            yield from self._chunks

    class FakeStream:
        def __init__(self, resp):
            self._resp = resp

        def __enter__(self):
            return self._resp

        def __exit__(self, *a):
            return False

    ns = SimpleNamespace(calls=[], routes={})

    class FakeClient:
        def __init__(self, transport=None, timeout=None, headers=None):
            assert transport is not None, "every fetch must use a pinned transport"

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, method, url, follow_redirects=False):
            assert follow_redirects is False, "redirects must be walked manually"
            ns.calls.append(url)
            if url in ns.routes:
                status, headers, chunks = ns.routes[url]
            else:
                status, headers, chunks = (
                    200,
                    {"content-type": "text/html; charset=utf-8"},
                    [DEFAULT_BODY],
                )
            return FakeStream(FakeResp(url, status, headers, chunks))

    monkeypatch.setattr(fetch_mod.httpx, "Client", FakeClient)
    return ns


def test_fetch_extracts_clean_text(fake_dns, fake_http):
    text = fetch_url("https://example.com/page")
    assert "Headline" in text
    assert "Body text here." in text
    assert "evil()" not in text  # script stripped
    assert ".x{}" not in text  # style stripped


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "https://10.0.0.5/",
        "https://192.168.1.1/",
        "https://172.16.0.9/",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://100.64.0.5/",  # carrier-grade NAT
        "http://0.0.0.0/",
        "http://[::1]/",
    ],
)
def test_ssrf_targets_rejected(fake_dns, fake_http, url):
    with pytest.raises(FetchError, match="non-public IP|only http"):
        fetch_url(url)


def test_cgnat_hostname_rejected(fake_dns, fake_http):
    fake_dns["cgnat.test"] = "100.64.0.5"
    with pytest.raises(FetchError, match="non-public IP"):
        fetch_url("http://cgnat.test/")


def test_non_http_scheme_rejected(fake_dns, fake_http):
    with pytest.raises(FetchError, match="only http"):
        fetch_url("ftp://example.com/file")


def test_unresolvable_host_rejected(fake_http, monkeypatch):
    def boom(host, port, *a, **k):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(FetchError, match="cannot resolve"):
        fetch_url("https://does-not-exist.invalid/")


def test_redirect_to_private_ip_rejected_and_never_requested(fake_dns, fake_http):
    # H-1: the 302 target is validated BEFORE any socket opens for it.
    fake_http.routes["https://example.com/redirect"] = (
        302,
        {"location": "http://127.0.0.1/internal"},
        [],
    )
    with pytest.raises(FetchError, match="non-public IP"):
        fetch_url("https://example.com/redirect")
    assert fake_http.calls == ["https://example.com/redirect"]


def test_redirect_chain_followed_within_hop_limit(fake_dns, fake_http):
    fake_http.routes["https://example.com/a"] = (
        302,
        {"location": "https://example.com/b"},
        [],
    )
    text = fetch_url("https://example.com/a")
    assert "Headline" in text
    assert fake_http.calls == ["https://example.com/a", "https://example.com/b"]


def test_redirect_loop_rejected(fake_dns, fake_http):
    fake_http.routes["https://example.com/loop"] = (
        302,
        {"location": "https://example.com/loop"},
        [],
    )
    with pytest.raises(FetchError, match="too many redirects"):
        fetch_url("https://example.com/loop")


def test_too_many_redirect_hops_rejected(fake_dns, fake_http):
    for i in range(7):
        fake_http.routes[f"https://example.com/h{i}"] = (
            302,
            {"location": f"https://example.com/h{i + 1}"},
            [],
        )
    with pytest.raises(FetchError, match="too many redirects"):
        fetch_url("https://example.com/h0")


def test_content_length_precheck_rejected(fake_dns, fake_http):
    fake_http.routes["https://example.com/big"] = (
        200,
        {"content-type": "text/html", "content-length": str(50 * 1024 * 1024)},
        [b"x"],
    )
    with pytest.raises(FetchError, match="content-length"):
        fetch_url("https://example.com/big")


def test_streaming_body_cap_rejected(fake_dns, fake_http):
    # H-3: no Content-Length; the running total still caps the body.
    fake_http.routes["https://example.com/drip"] = (
        200,
        {"content-type": "text/html"},
        [b"y" * 65536] * 100,  # 6.5 MB of decoded bytes
    )
    with pytest.raises(FetchError, match="exceeds .* byte limit"):
        fetch_url("https://example.com/drip")


def test_pinned_backend_dials_validated_ip():
    # H-2: the TCP dial uses the vetted IP literal, never the URL hostname.
    dialed = []

    class FakeInner:
        def connect_tcp(self, host, port, timeout=None, local_address=None,
                        socket_options=None):
            dialed.append(host)
            return object()

        def sleep(self, seconds):
            pass

    backend = fetch_mod._PinnedIPBackend("93.184.216.34")
    backend._inner = FakeInner()
    backend.connect_tcp("example.com", 443)
    assert dialed == ["93.184.216.34"]


def test_non_html_content_rejected(fake_dns, fake_http):
    fake_http.routes["https://example.com/f.pdf"] = (
        200,
        {"content-type": "application/pdf"},
        [b"%PDF-1.4"],
    )
    with pytest.raises(FetchError, match="unsupported content type"):
        fetch_url("https://example.com/f.pdf")


def test_http_error_status_rejected(fake_dns, fake_http):
    fake_http.routes["https://example.com/missing"] = (
        404,
        {"content-type": "text/html"},
        [b"<p>gone</p>"],
    )
    with pytest.raises(FetchError, match="HTTP 404"):
        fetch_url("https://example.com/missing")


def test_connection_failure_wrapped(fake_dns, fake_http, monkeypatch):
    class BoomClient:
        def __init__(self, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def stream(self, method, url, follow_redirects=False):
            raise httpx.ConnectError("refused")

    monkeypatch.setattr(fetch_mod.httpx, "Client", BoomClient)
    with pytest.raises(FetchError, match="fetch failed"):
        fetch_url("https://example.com/")


def test_empty_text_rejected(fake_dns, fake_http):
    fake_http.routes["https://example.com/empty"] = (
        200,
        {"content-type": "text/html"},
        [b"<html><body><script>var x=1;</script></body></html>"],
    )
    with pytest.raises(FetchError, match="no extractable text"):
        fetch_url("https://example.com/empty")
