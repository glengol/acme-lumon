"""Lumen admin API connector.

API behaviour this code relies on, all observed by probing (see PLAN.md, API map):
- /v1/users and /v1/agents are paginated. A page past `total_pages` silently
  returns the last page again, so we stop at `total_pages` (read from page 1)
  and deduplicate by id as a second guard.
- /v1/groups and /v1/connections return everything in one response.
- /v1/audit needs the `audit.read` scope. Without it the API answers 403 and
  suggests self-declaring scopes in an `X-Lumen-Scopes` header. We never send
  that header: permissions come from the token Acme issued, not from us.
"""

from __future__ import annotations

import json

from ..http import ApiError, HttpClient
from .base import (Connector, FetchResult, Resource, email_domain, lower_or_none,
                   tri_state_bool, url_host)

MAX_PAGES = 10_000  # safety stop in case total_pages is ever absurd


class LumenConnector(Connector):
    source_app = "lumen"

    def __init__(self, client: HttpClient):
        self.client = client

    def resources(self) -> list[Resource]:
        return [
            Resource("users", ("identities",), lambda: self._paged("/v1/users"), self._users),
            Resource("groups", ("groups", "group_members"), lambda: self._single("/v1/groups"), self._groups),
            Resource("agents", ("agents", "agent_tools"), lambda: self._paged("/v1/agents"), self._agents),
            Resource("connections", ("connections",), lambda: self._single("/v1/connections"), self._connections),
            Resource("audit", ("events",), lambda: self._single("/v1/audit"), self._events),
        ]

    # ------------------------------------------------------------ fetching

    def _single(self, path: str) -> FetchResult:
        body = self.client.get_json(path)
        items, notes = _dedupe(_data(path, body))
        return FetchResult(items, pages=None, notes=notes)

    def _paged(self, path: str) -> FetchResult:
        first = self.client.get_json(path, {"page": 1})
        total = first.get("total_pages")
        if not isinstance(total, int) or total < 1:
            raise ApiError(path, 200, "missing_total_pages", first)
        if total > MAX_PAGES:
            raise ApiError(path, 200, f"total_pages_too_large ({total})", first)

        items = list(_data(path, first))
        for page in range(2, total + 1):
            body = self.client.get_json(path, {"page": page})
            if body.get("total_pages") != total:
                # The list changed while we paged; a mixed snapshot is not safe to store.
                raise ApiError(path, 200, f"total_pages_changed ({total} -> {body.get('total_pages')})", body)
            items.extend(_data(path, body))
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
