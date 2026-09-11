"""HTTP transport with egress control, retries and caching (SRS 4.1, PRIV-004).

urllib is used deliberately: it is standard library, honours the system trust
store, and keeps the dependency surface small.  The transport is the only place
that opens a socket, so the privacy allow-list and the offline switch have a
single choke point.
"""

from __future__ import annotations

import hashlib
import json
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .privacy import EgressPolicy

__all__ = [
    "HttpResponse",
    "ResponseCache",
    "TransportError",
    "UrllibTransport",
]

_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class TransportError(RuntimeError):
    """Raised when a request fails after retries, or is refused."""

    def __init__(self, message: str, *, status: int | None = None, url: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.url = url


@dataclass(slots=True)
class HttpResponse:
    status: int
    body: str
    headers: dict[str, str] = field(default_factory=dict)
    url: str = ""

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class UrllibTransport:
    """Performs HTTPS requests after checking the egress allow-list."""

    def __init__(
        self,
        policy: EgressPolicy | None = None,
        *,
        user_agent: str = "qslcard/0.1 (local-only)",
        max_retries: int = 3,
        backoff: float = 1.0,
        timeout: float = 30.0,
        sleeper: Callable[[float], None] = time.sleep,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.policy = policy or EgressPolicy()
        self.user_agent = user_agent
        self.max_retries = max_retries
        self.backoff = backoff
        self.timeout = timeout
        self._sleep = sleeper
        self._opener = opener
        self._context = ssl.create_default_context()

    def request(
        self,
        method: str,
        url: str,
        *,
        data: Mapping[str, str] | bytes | None = None,
        headers: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        host = self.policy.check(url)
        body: bytes | None
        if isinstance(data, bytes) or data is None:
            body = data
        else:
            body = urllib.parse.urlencode(data).encode("ascii")
        request_headers = {"User-Agent": self.user_agent, "Host": host}
        if headers:
            request_headers.update(headers)
        if body is not None and "Content-Type" not in request_headers:
            request_headers["Content-Type"] = "application/x-www-form-urlencoded"

        attempt = 0
        while True:
            attempt += 1
            request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
            try:
                open_call = self._opener or urllib.request.urlopen
                with open_call(
                    request, timeout=timeout or self.timeout, context=self._context
                ) as response:
                    payload = response.read().decode("utf-8", "replace")
                    return HttpResponse(
                        status=int(getattr(response, "status", 200)),
                        body=payload,
                        headers=dict(getattr(response, "headers", {}) or {}),
                        url=url,
                    )
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace") if hasattr(exc, "read") else ""
                if exc.code in _RETRY_STATUS and attempt <= self.max_retries:
                    self._sleep(self.backoff * (2 ** (attempt - 1)))
                    continue
                raise TransportError(
                    f"HTTP {exc.code} from {host}: {detail[:200]}", status=exc.code, url=url
                ) from exc
            except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
                if attempt <= self.max_retries:
                    self._sleep(self.backoff * (2 ** (attempt - 1)))
                    continue
                raise TransportError(f"request to {host} failed: {exc}", url=url) from exc

    def get(self, url: str, **kwargs: Any) -> HttpResponse:
        return self.request("GET", url, **kwargs)

    def post(
        self, url: str, data: Mapping[str, str] | bytes | None = None, **kwargs: Any
    ) -> HttpResponse:
        return self.request("POST", url, data=data, **kwargs)


@dataclass(slots=True)
class ResponseCache:
    """Small on-disk cache so callbook lookups survive restarts (SR-COM-004).

    ttl_seconds of 0 expires entries immediately; a negative value disables
    expiry entirely (useful for callsigns that never change).
    """

    directory: str
    ttl_seconds: int = 30 * 24 * 3600

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return Path(self.directory) / f"{digest}.json"

    def get(self, key: str) -> str | None:
        path = self._path(key)
        if not path.is_file():
            return None
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if self.ttl_seconds >= 0 and time.time() - float(document.get("at", 0)) > self.ttl_seconds:
            return None
        value = document.get("value")
        return value if isinstance(value, str) else None

    def put(self, key: str, value: str) -> None:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"at": time.time(), "key": key, "value": value}, ensure_ascii=False)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(path)

    def clear(self) -> int:
        removed = 0
        directory = Path(self.directory)
        if directory.is_dir():
            for item in directory.glob("*.json"):
                item.unlink()
                removed += 1
        return removed
