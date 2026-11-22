"""Lumen admin API connector.

API behaviour this code relies on, all observed by probing (see PLAN.md, API map):
- /v1/users and /v1/agents are paginated. A page past `total_pages` silently
  returns the last page again, so we stop at `total_pages` (read from page 1)
  and deduplicate by id as a second guard.
- /v1/groups and /v1/connections return everything in one response.
- /v1/audit needs the `audit.read` scope. Without it the API answers 403 and
  suggests self-declaring scopes in an `X-Lumen-Scopes` header. We never send
  that header: permissions come from the token Acme issued, not from us.
- Audit is a log, so it is collected incrementally (`since` = newest stored
  event minus a 24 h overlap; an explicit epoch on the first run) and stored
  append-only. Events that age out of Lumen's retention are kept here. If a
  response carries `total_pages`, every page is fetched, and the page count
  may grow while we read (a live log), which is tolerated for append-only data.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from ..http import ApiError, HttpClient
from .base import (Connector, FetchResult, Resource, email_domain, lower_or_none,
                   tri_state_bool, url_host)

MAX_PAGES = 10_000                     # safety stop in case total_pages is ever absurd
# Re-read a day before the newest stored event, so events that are logged late
# (with an older timestamp) are still picked up. Re-read events land on the
# same row (event id is the key), so the overlap costs bandwidth, not correctness.
AUDIT_OVERLAP = timedelta(hours=24)
AUDIT_EPOCH = "1970-01-01T00:00:00Z"   # explicit "from the beginning" for the first collection


class LumenConnector(Connector):
    source_app = "lumen"

    def __init__(self, client: HttpClient):
        self.client = client

    def resources(self) -> list[Resource]:
        return [
            Resource("users", ("identities",), lambda _: self._paged("/v1/users"), self._users),
            Resource("groups", ("groups", "group_members"), lambda _: self._single("/v1/groups"), self._groups),
            Resource("agents", ("agents", "agent_tools"), lambda _: self._paged("/v1/agents"), self._agents),
            Resource("connections", ("connections",), lambda _: self._single("/v1/connections"), self._connections),
            Resource("audit", ("events",), self._audit, self._events,
                     mode="append", cursor=("events", "occurred_at")),
        ]

    # ------------------------------------------------------------ fetching

    def _single(self, path: str, params: dict | None = None, strict: bool = True) -> FetchResult:
        """One call. If the response turns out to be paginated, follow it."""
        body = self.client.get_json(path, params)
        if "total_pages" in body:
            return self._paged(path, params, first=body, strict=strict)
        items, notes = _dedupe(_data(path, body))
        return FetchResult(items, pages=None, notes=notes)

    def _audit(self, cursor: str | None) -> FetchResult:
        if cursor is None:
            since_text, label = AUDIT_EPOCH, "full history"
        else:
            since_text = (_parse_time(cursor) - AUDIT_OVERLAP).strftime("%Y-%m-%dT%H:%M:%SZ")
            label = "incremental"
        result = self._single("/v1/audit", {"since": since_text}, strict=False)
        result.notes.append(f"{label} since {since_text}")
        return result

    def _paged(self, path: str, params: dict | None = None, first: dict | None = None,
               strict: bool = True) -> FetchResult:
        """Read pages 1..total_pages.

        strict=True (snapshots): abort if total_pages changes mid-read, because a
        mixed snapshot is not safe to store. strict=False (append-only logs): a
        growing log is normal, so keep reading up to the newest page count.
        """
        params = dict(params or {})
        if first is None:
            first = self.client.get_json(path, {**params, "page": 1})
        total = first.get("total_pages")
        if not isinstance(total, int) or total < 1:
            raise ApiError(path, 200, "missing_total_pages", first)
        if total > MAX_PAGES:
            raise ApiError(path, 200, f"total_pages_too_large ({total})", first)

        items = list(_data(path, first))
        page = 2
        while page <= total:
            body = self.client.get_json(path, {**params, "page": page})
            now_total = body.get("total_pages")
            if now_total != total:
                if strict or not isinstance(now_total, int) or now_total > MAX_PAGES:
                    raise ApiError(path, 200, f"total_pages_changed ({total} -> {now_total})", body)
                total = max(total, now_total)
            items.extend(_data(path, body))
            page += 1
        items, notes = _dedupe(items)
        return FetchResult(items, pages=total, notes=notes)

    # ------------------------------------------------------------ mapping

    def _users(self, items):
        return {"identities": [{
            "external_id": u["id"],
            "display_name": u.get("display_name"),
            "login": u.get("login"),
            "email": u.get("email"),
            "email_domain": email_domain(u.get("email")),
            "kind": lower_or_none(u.get("type")),
            "status_raw": u.get("status"),
            "status": lower_or_none(u.get("status")),
            "mfa_enabled": tri_state_bool(u.get("mfa_enabled")),
            "last_login_at": u.get("last_login_at"),
        } for u in items]}

    def _groups(self, items):
        groups, members = [], []
        for g in items:
            groups.append({"external_id": g["id"], "name": g.get("name")})
            for m in g.get("members") or []:
                if isinstance(m, dict) and m.get("id"):
                    members.append({"group_id": g["id"], "member_type": lower_or_none(m.get("type")) or "unknown",
                                    "member_id": m["id"]})
        return {"groups": groups, "group_members": _unique(members, ("group_id", "member_type", "member_id"))}

    def _agents(self, items):
        agents, tools = [], []
        for a in items:
            agents.append({"external_id": a["id"], "name": a.get("name"), "owner_id": a.get("owner_id"),
                           "status_raw": a.get("status"), "status": lower_or_none(a.get("status"))})
            for t in a.get("tools") or []:
                if not isinstance(t, dict) or not t.get("id"):
                    continue
                kind = lower_or_none(t.get("kind"))
                tools.append({
                    "agent_id": a["id"], "tool_id": t["id"], "kind": kind, "target": t.get("target"),
                    "target_host": url_host(t.get("target")) if kind == "http" else None,
                    "connection_id": t.get("connection_id"),
                    "scopes": json.dumps(t.get("scopes") or []),
                })
        return {"agents": agents, "agent_tools": _unique(tools, ("agent_id", "tool_id"))}

    def _connections(self, items):
        return {"connections": [{
            "external_id": c["id"], "name": c.get("name"), "app": lower_or_none(c.get("app")),
            "authorized_by": c.get("authorized_by"), "scopes": json.dumps(c.get("scopes") or []),
            "last_used_at": c.get("last_used_at"),
        } for c in items]}

    def _events(self, items):
        return {"events": [{
            "external_id": e["id"], "occurred_at": e.get("time"), "action": e.get("action"),
            "actor_id": e.get("actor_id"), "target_id": e.get("target_id"), "detail": e.get("detail"),
        } for e in items]}


# ---------------------------------------------------------------- helpers

def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _data(path: str, body: dict) -> list:
    data = body.get("data")
    if not isinstance(data, list):
        raise ApiError(path, 200, "missing_data_array", body)
    return data


def _dedupe(items: list) -> tuple[list[dict], list[str]]:
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


def _unique(rows: list[dict], key: tuple[str, ...]) -> list[dict]:
    seen, out = set(), []
    for row in rows:
        k = tuple(row[f] for f in key)
        if k not in seen:
            seen.add(k)
            out.append(row)
    return out
