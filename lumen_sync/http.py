"""Minimal JSON-over-HTTP client (standard library only)."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request


class ApiError(Exception):
    """A request that did not produce usable data. Carries enough to explain why."""

    def __init__(self, path: str, http_status: int | None, error: str, body: dict | None = None):
        self.path = path
        self.http_status = http_status
        self.error = error
        self.body = body or {}
        super().__init__(f"{path}: {http_status} {error}")


class HttpClient:
    """GET JSON with bearer auth, a timeout, and retries for transient failures only.

    Retried: network errors, 429 and 5xx (with backoff).
    Not retried: other 4xx. They will not succeed on a second try, and retrying
    a 401/403 just hammers the customer's API.
    """

    def __init__(self, base_url: str, token: str, timeout: float = 15.0,
                 retries: int = 3, backoff: float = 0.5):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff

    def get_json(self, path: str, params: dict | None = None) -> dict:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
            "User-Agent": "lumen-sync/1.0",
        })

        last_error: ApiError | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(self.backoff * (2 ** (attempt - 1)))
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    return self._parse(path, response.status, response.read())
            except urllib.error.HTTPError as exc:
                body = self._safe_json(exc.read())
                last_error = ApiError(path, exc.code, body.get("error", exc.reason or "http_error"), body)
                if exc.code != 429 and exc.code < 500:
                    raise last_error
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                reason = getattr(exc, "reason", exc)
                last_error = ApiError(path, None, f"network_error: {reason}")
        assert last_error is not None
        raise last_error

    def _parse(self, path: str, status: int, raw: bytes) -> dict:
        body = self._safe_json(raw)
        if not isinstance(body, dict) or "_unparseable" in body:
            raise ApiError(path, status, "invalid_json")
        return body

    @staticmethod
    def _safe_json(raw: bytes) -> dict:
        try:
            value = json.loads(raw.decode("utf-8") or "{}")
            return value if isinstance(value, dict) else {"_unparseable": True}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {"_unparseable": True}
