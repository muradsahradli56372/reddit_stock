"""Small HTTP helper for public JSON APIs (ApeWisdom, StockTwits).

* Retries 429 / 5xx / network errors with exponential backoff (2s, 4s, 8s, ...), honouring a
  Retry-After header when the server sends one.
* 4xx other than 429 are NOT retried (wrong URL, blocked, gone) - they raise HttpError.
* The transport is injectable so tests never touch the network.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

import httpx

from .config import settings

log = logging.getLogger(__name__)


class HttpError(RuntimeError):
    def __init__(self, url: str, status: int, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.url, self.status = url, status


class JsonClient:
    def __init__(self, transport: httpx.BaseTransport | None = None, sleep: Callable[[float], Any] = time.sleep,
                 max_retries: int | None = None, timeout: float = 20.0):
        self._client = httpx.Client(transport=transport, timeout=timeout, follow_redirects=True,
                                    headers={"User-Agent": settings.http_user_agent, "Accept": "application/json"})
        self._sleep = sleep
        self.max_retries = max_retries or settings.reddit_max_retries
        self.requests = 0

    def get_json(self, url: str, params: dict | None = None) -> Any:
        for attempt in range(self.max_retries):
            self.requests += 1
            try:
                r = self._client.get(url, params=params)
            except httpx.HTTPError as exc:
                if attempt == self.max_retries - 1:
                    raise
                delay = 2.0 * 2 ** attempt
                log.warning("http error, retrying", extra={"url": url, "error": repr(exc), "delay_s": delay})
                self._sleep(delay)
                continue
            if r.status_code == 200:
                return r.json()
            if r.status_code == 429 or r.status_code >= 500:
                if attempt == self.max_retries - 1:
                    raise HttpError(url, r.status_code, r.text)
                retry_after = r.headers.get("Retry-After")
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2.0 * 2 ** attempt
                log.warning("rate limited / server error, retrying",
                            extra={"url": url, "status": r.status_code, "delay_s": delay})
                self._sleep(min(delay, 300))
                continue
            raise HttpError(url, r.status_code, r.text)
        raise RuntimeError("unreachable")
