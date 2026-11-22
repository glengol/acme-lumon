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


MAX_RETRY_AFTER = 60.0  # seconds; never let a server stall a run indefinitely


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects. Following one would resend the bearer token to wherever
    the server points, possibly another host. An admin API has no reason to redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # urllib then raises HTTPError for the 3xx, which we report


class HttpClient:
    """GET JSON with bearer auth, a timeout, and retries for transient failures only.

    Retried: network errors, 429 and 5xx. Backoff doubles each attempt, unless
    the server sends Retry-After (seconds), which is honoured up to 60 s.
    Not retried: other 4xx. They will not succeed on a second try, and retrying
    a 401/403 just hammers the customer's API.
    Redirects are refused, so the token is only ever sent to base_url.
    """

    def __init__(self, base_url: str, token: str, timeout: float = 15.0,
                 retries: int = 3, backoff: float = 0.5):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff
        self._opener = urllib.request.build_opener(_NoRedirect)

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
        wait: float | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(wait if wait is not None else self.backoff * (2 ** (attempt - 1)))
            wait = None
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    return self._parse(path, response.status, response.read())
            except urllib.error.HTTPError as exc:
                if 300 <= exc.code < 400:
                    raise ApiError(path, exc.code, f"redirect_refused (to {exc.headers.get('Location')})")
                body = self._safe_json(exc.read())
                last_error = ApiError(path, exc.code, body.get("error", exc.reason or "http_error"), body)
                if exc.code != 429 and exc.code < 500:
                    raise last_error
                wait = _retry_after(exc.headers.get("Retry-After"))
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


def _retry_after(value) -> float | None:
    """Retry-After in seconds (the HTTP-date form is ignored), capped at MAX_RETRY_AFTER."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(seconds, MAX_RETRY_AFTER))
