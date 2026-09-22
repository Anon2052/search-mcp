"""Tests for fetch_url: SSRF guard, text extraction, failure modes."""

from __future__ import annotations

import ipaddress
import socket

import httpx
import pytest

from search_mcp import fetch as fetch_mod
from search_mcp.fetch import FetchError, fetch_url


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
    calls = []

    class FakeResp:
        def __init__(self, url, body, ctype="text/html; charset=utf-8", status=200):
            self.url = url
            self.content = body
            self.headers = {"content-type": ctype}
            self.status_code = status
            self.encoding = "utf-8"

    def fake_get(url, **kwargs):
        calls.append(url)
        return FakeResp(
            url,
            b"<html><head><title>T</title></head><body>"
            b"<script>evil()</script><style>.x{}</style>"
            b"<h1>Headline</h1><p>Body text here.</p></body></html>",
        )

    monkeypatch.setattr(fetch_mod.httpx, "get", fake_get)
    return calls


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
        "http://0.0.0.0/",
        "http://[::1]/",
    ],
)
def test_ssrf_targets_rejected(fake_dns, fake_http, url):
    with pytest.raises(FetchError, match="non-public IP|only http"):
        fetch_url(url)


def test_non_http_scheme_rejected(fake_dns, fake_http):
    with pytest.raises(FetchError, match="only http"):
        fetch_url("ftp://example.com/file")


def test_unresolvable_host_rejected(fake_http, monkeypatch):
    def boom(host, port, *a, **k):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    with pytest.raises(FetchError, match="cannot resolve"):
        fetch_url("https://does-not-exist.invalid/")


def test_redirect_to_private_ip_rejected(fake_dns, fake_http, monkeypatch):
    class RedirectResp:
        url = "http://127.0.0.1/internal"
        content = b"<p>x</p>"
        headers = {"content-type": "text/html"}
        status_code = 200
        encoding = "utf-8"

    monkeypatch.setattr(fetch_mod.httpx, "get", lambda url, **k: RedirectResp())
    with pytest.raises(FetchError, match="non-public IP"):
        fetch_url("https://example.com/redirect")


def test_non_html_content_rejected(fake_dns, monkeypatch):
    class PdfResp:
        url = "https://example.com/f.pdf"
        content = b"%PDF-1.4"
        headers = {"content-type": "application/pdf"}
        status_code = 200
        encoding = "utf-8"

    monkeypatch.setattr(fetch_mod.httpx, "get", lambda url, **k: PdfResp())
    with pytest.raises(FetchError, match="unsupported content type"):
        fetch_url("https://example.com/f.pdf")


def test_http_error_status_rejected(fake_dns, monkeypatch):
    class NotFound:
        url = "https://example.com/missing"
        content = b"<p>gone</p>"
        headers = {"content-type": "text/html"}
        status_code = 404
        encoding = "utf-8"

    monkeypatch.setattr(fetch_mod.httpx, "get", lambda url, **k: NotFound())
    with pytest.raises(FetchError, match="HTTP 404"):
        fetch_url("https://example.com/missing")


def test_connection_failure_wrapped(fake_dns, monkeypatch):
    def boom(url, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(fetch_mod.httpx, "get", boom)
    with pytest.raises(FetchError, match="fetch failed"):
        fetch_url("https://example.com/")


def test_empty_text_rejected(fake_dns, monkeypatch):
    class Empty:
        url = "https://example.com/empty"
        content = b"<html><body><script>var x=1;</script></body></html>"
        headers = {"content-type": "text/html"}
        status_code = 200
        encoding = "utf-8"

    monkeypatch.setattr(fetch_mod.httpx, "get", lambda url, **k: Empty())
    with pytest.raises(FetchError, match="no extractable text"):
        fetch_url("https://example.com/empty")
