"""The contract every source app implements.

A connector knows how to talk to one app and how to map its objects into the
app-neutral tables. The runner and the store know nothing app-specific, so a
second app is one new file in this folder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse


@dataclass
class FetchResult:
    items: list[dict]                       # raw objects, deduplicated by id
    pages: int | None = None                # pages fetched, for paginated resources
    notes: list[str] = field(default_factory=list)  # anything odd worth showing in coverage


@dataclass
class Resource:
    name: str                               # e.g. "users"; also the coverage key
    tables: tuple[str, ...]                 # tables this resource owns (replaced together)
    fetch: Callable[[], FetchResult]
    to_rows: Callable[[list[dict]], dict[str, list[dict]]]  # raw items -> {table: rows}


class Connector:
    source_app: str = ""

    def resources(self) -> list[Resource]:
        raise NotImplementedError


# ---------------------------------------------------------------- shared helpers

def lower_or_none(value) -> str | None:
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def email_domain(email) -> str | None:
    if isinstance(email, str) and "@" in email:
        return email.rsplit("@", 1)[1].strip().lower() or None
    return None


def url_host(url) -> str | None:
    if not isinstance(url, str):
        return None
    try:
        return (urlparse(url).hostname or "").lower() or None
    except ValueError:
        return None


def tri_state_bool(value) -> int | None:
    """True -> 1, False -> 0, anything else (missing, null, odd type) -> None (unknown)."""
    if value is True:
        return 1
    if value is False:
        return 0
    return None
