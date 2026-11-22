"""Paging and dedupe helpers any HTTP connector can reuse.

Behaviour (observed on Lumen, and safe for most list APIs):
- `total_pages` from the first response decides how many pages to read. Pages
  past the end are never requested (Lumen silently repeats the last page).
- Objects are deduplicated by `id`; objects without one are counted, not stored.
- strict=True (snapshots): if `total_pages` changes mid-read, abort, because a
  mixed snapshot is not safe to store.
- strict=False (append-only logs): a log may grow or shrink while we read. If
  the page count changes, read every page a second time and merge, so items
  that shifted between pages (for example when retention drops old events)
  are not skipped. Duplicates are harmless because the id is the key.
"""

from __future__ import annotations

from ..http import ApiError, HttpClient
from .base import FetchResult

MAX_PAGES = 10_000  # safety stop in case total_pages is ever absurd


def fetch_list(client: HttpClient, path: str, params: dict | None = None, strict: bool = True) -> FetchResult:
    """One call. If the response turns out to be paginated, follow every page."""
    body = client.get_json(path, params)
    if "total_pages" in body:
        return fetch_pages(client, path, params, first=body, strict=strict)
    items, notes = dedupe(data(path, body))
    return FetchResult(items, pages=None, notes=notes)


def fetch_pages(client: HttpClient, path: str, params: dict | None = None, first: dict | None = None,
                strict: bool = True) -> FetchResult:
    params = dict(params or {})
    items, total, changed = _read_all(client, path, params, first, strict)
    notes = []
    if changed:
        again, total, _ = _read_all(client, path, params, None, strict)
        items += again
        notes.append("page count changed while reading; read twice and merged")
    items, dedupe_notes = dedupe(items)
    return FetchResult(items, pages=total, notes=notes + dedupe_notes)


def _read_all(client, path, params, first, strict):
    if first is None:
        first = client.get_json(path, {**params, "page": 1})
    total = _total(path, first)
    items, changed, page = list(data(path, first)), False, 2
    while page <= total:
        body = client.get_json(path, {**params, "page": page})
        now = body.get("total_pages")
        if now != total:
            if strict:
                raise ApiError(path, 200, f"total_pages_changed ({total} -> {now})", body)
            changed = True
            total = max(total, _total(path, body))
        items.extend(data(path, body))
        page += 1
    return items, total, changed


def _total(path: str, body: dict) -> int:
    total = body.get("total_pages")
    if not isinstance(total, int) or total < 1:
        raise ApiError(path, 200, "missing_total_pages", body)
    if total > MAX_PAGES:
        raise ApiError(path, 200, f"total_pages_too_large ({total})", body)
    return total


def data(path: str, body: dict) -> list:
    value = body.get("data")
    if not isinstance(value, list):
        raise ApiError(path, 200, "missing_data_array", body)
    return value


def dedupe(items: list) -> tuple[list[dict], list[str]]:
    """Keep the first object per id. Objects without an id cannot be stored safely, so they are counted."""
    seen, kept, no_id, dupes = set(), [], 0, 0
    for item in items:
        item_id = item.get("id") if isinstance(item, dict) else None
        if not item_id:
            no_id += 1
        elif item_id in seen:
            dupes += 1
        else:
            seen.add(item_id)
            kept.append(item)
    notes = []
    if dupes:
        notes.append(f"{dupes} duplicate id(s) dropped")
    if no_id:
        notes.append(f"{no_id} object(s) without id skipped")
    return kept, notes
