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
    """One thing to collect.

    mode "replace": the source returns the full current state; each successful
    fetch replaces this resource's rows (users, groups, agents, ...).
    mode "append": the source is a log; rows are only ever added or updated,
    never deleted, so history survives source-side retention (audit events).
    An append resource names a cursor (table, column): the newest value already
    stored is passed to fetch() so only newer records are requested.
    """
    name: str                               # e.g. "users"; also the coverage key
    tables: tuple[str, ...]                 # tables this resource owns
    fetch: Callable[[str | None], FetchResult]   # receives the cursor value (None = fetch everything)
    to_rows: Callable[[list[dict]], dict[str, list[dict]]]  # raw items -> {table: rows}, plus optional NOTES
    mode: str = "replace"                   # replace | append
    cursor: tuple[str, str] | None = None   # (table, column) holding the newest stored value


class Connector:
    source_app: str = ""

    def resources(self) -> list[Resource]:
        raise NotImplementedError


# to_rows may add this key with a list of strings: things it skipped or changed
# while mapping. They are shown in the coverage report, so nothing is dropped silently.
NOTES = "notes"


# ---------------------------------------------------------------- neutral vocabulary
# Connectors map their app's own words onto these, so views and queries stay app-neutral.

IDENTITY_HUMAN = "human"
IDENTITY_SERVICE = "service"
TOOL_APP_CONNECTION = "app_connection"   # reaches another app through a stored or built-in login
TOOL_OUTBOUND_URL = "outbound_url"       # sends requests to an arbitrary URL (webhook, HTTP call)


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
