"""IPv4-first HTTP/proxy transport, deadline reader, multipart encoder, and JPEG compressor."""

from __future__ import annotations

import http.client
import io
import socket
import time
from typing import Any
import urllib.error
import urllib.request
import uuid

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)


class RateLimited(Exception):
    pass


class _EngineSkipped(RateLimited):
    """Engine call skipped due to active rate-limit backoff."""


def _multipart(field_name: str, raw: bytes, extra: dict[str, str] | None = None) -> tuple[bytes, str]:
    boundary = "----AnimeAlert" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for key, value in (extra or {}).items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    chunks.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{field_name}"; filename="image.jpg"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
    )
    chunks.append(raw)
    chunks.append(f"\r\n--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def _create_connection_ipv4_first(
    address: tuple[str, int],
    timeout: Any = socket._GLOBAL_DEFAULT_TIMEOUT,
    source_address: tuple[str, int] | None = None,
) -> socket.socket:
    """Prefer IPv4 addresses to avoid unreachable IPv6 routes inside containers."""
    host, port = address
    try:
        addr_infos = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)
    except OSError:
        addr_infos = []
    if not addr_infos:
        return socket.create_connection(address, timeout, source_address)

    last_exc: OSError | None = None
    for family, socktype, proto, _, sockaddr in addr_infos:
        sock: socket.socket | None = None
        try:
            sock = socket.socket(family, socktype, proto)
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                sock.settimeout(timeout)
            if source_address:
                sock.bind(source_address)
            sock.connect(sockaddr)
            return sock
        except OSError as exc:
            last_exc = exc
            if sock is not None:
                sock.close()
    assert last_exc is not None
    raise last_exc


class _IPv4HTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _create_connection_ipv4_first


class _IPv4HTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._create_connection = _create_connection_ipv4_first


class _IPv4HTTPHandler(urllib.request.HTTPHandler):
    def http_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_IPv4HTTPConnection, req)


class _IPv4HTTPSHandler(urllib.request.HTTPSHandler):
    def https_open(self, req: urllib.request.Request) -> http.client.HTTPResponse:
        return self.do_open(_IPv4HTTPSConnection, req)


_OPENER = urllib.request.build_opener(_IPv4HTTPHandler(), _IPv4HTTPSHandler())
_PROXY_OPENERS: dict[str, urllib.request.OpenerDirector] = {}


def _build_opener(proxy_url: str = "") -> urllib.request.OpenerDirector:
    proxy = str(proxy_url or "").strip()
    if not proxy:
        return _OPENER
    cached = _PROXY_OPENERS.get(proxy)
    if cached is not None:
        return cached
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
        _IPv4HTTPHandler(),
        _IPv4HTTPSHandler(),
    )
    if len(_PROXY_OPENERS) >= 8:
        _PROXY_OPENERS.clear()
    _PROXY_OPENERS[proxy] = opener
    return opener


def _compress_jpeg(raw: bytes, max_dim: int = 1200, quality: int = 85) -> bytes:
    try:
        from PIL import Image

        im = Image.open(io.BytesIO(raw)).convert("RGB")
        im.thumbnail((max_dim, max_dim))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
    except Exception:
        return raw


def _set_response_socket_timeout(resp: Any, seconds: float) -> None:
    try:
        raw = getattr(getattr(resp, "fp", None), "raw", None)
        sock = getattr(raw, "_sock", None)
        if sock is not None and hasattr(sock, "settimeout"):
            sock.settimeout(max(0.2, seconds))
    except Exception:
        pass


def _read_with_deadline(resp: Any, deadline: float, url: str, max_bytes: int = 4 * 1024 * 1024) -> bytes:
    chunks: list[bytes] = []
    total = 0
    read_fn = getattr(resp, "read1", None) or resp.read
    while total < max_bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"request deadline exceeded: {url}")
        _set_response_socket_timeout(resp, remaining)
        chunk = read_fn(min(16384, max_bytes - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _http(
    url: str,
    *,
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 20.0,
    proxy_url: str = "",
) -> bytes:
    req = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
        method="POST" if data else "GET",
    )
    deadline = time.monotonic() + float(timeout)
    opener = _build_opener(proxy_url)
    try:
        with opener.open(req, timeout=max(0.2, deadline - time.monotonic())) as resp:
            return _read_with_deadline(resp, deadline, url)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise RateLimited(url) from exc
        raise
