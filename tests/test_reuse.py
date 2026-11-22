"""Proof that a second app reuses the shape without a rewrite.

"Nimbus" is an invented AI platform with its own vocabulary and layout:
- people and bots come from two separate endpoints, both feeding `identities`
- tools are called "integration" and "webhook" instead of Lumen's words
- admins are a group called "Owners"

Only a connector (below, ~40 lines) is new. The runner, store, schema, views
and Q1-Q3 are the same code Lumen uses, and both apps share one database.

Run: python3 -m unittest -v
"""

from __future__ import annotations

import json
import unittest

from lumen_sync.connectors.base import (IDENTITY_HUMAN, IDENTITY_SERVICE, TOOL_APP_CONNECTION,
                                        TOOL_OUTBOUND_URL, Connector, FetchResult, Resource,
                                        email_domain, url_host)
from lumen_sync.report import QUERIES
from lumen_sync.runner import run_sync
from lumen_sync.store import Store

NIMBUS_TOOL_KINDS = {"integration": TOOL_APP_CONNECTION, "webhook": TOOL_OUTBOUND_URL}


class NimbusConnector(Connector):
    """A whole second connector. Data is in memory instead of HTTP to keep the test self-contained."""

    source_app = "nimbus"

    def __init__(self, data: dict):
        self.data = data

    def resources(self):
        get = lambda key: (lambda _cursor: FetchResult(list(self.data[key])))
        return [
            Resource("people", ("identities",), get("people"), self._people),
            Resource("bots", ("identities",), get("bots"), self._bots),      # same table, second resource
            Resource("teams", ("groups", "group_members"), get("teams"), self._teams),
            Resource("assistants", ("agents", "agent_tools"), get("assistants"), self._assistants),
            Resource("grants", ("connections",), get("grants"), self._grants),
        ]

    def _people(self, items):
        return {"identities": [{
            "external_id": p["id"], "display_name": p["name"], "email": p["mail"],
            "email_domain": email_domain(p["mail"]), "kind": IDENTITY_HUMAN,
            "status_raw": "enabled" if p["enabled"] else "disabled",
            "status": "active" if p["enabled"] else "disabled",
            "mfa_enabled": int(p["two_factor"]), "last_login_at": p["seen"],
        } for p in items]}

    def _bots(self, items):
        return {"identities": [{"external_id": b["id"], "display_name": b["name"], "kind": IDENTITY_SERVICE,
                                "status_raw": "enabled", "status": "active"} for b in items]}

    def _teams(self, items):
        return {"groups": [{"external_id": t["id"], "name": t["title"]} for t in items],
                "group_members": [{"group_id": t["id"], "member_type": "user", "member_id": m}
                                  for t in items for m in t["people"]]}

    def _assistants(self, items):
        tools = []
        for a in items:
            for i, c in enumerate(a["capabilities"]):
                kind = NIMBUS_TOOL_KINDS.get(c["type"], c["type"])
                tools.append({"agent_id": a["id"], "tool_id": f"{a['id']}-{i}", "kind": kind, "kind_raw": c["type"],
                              "target": c["to"], "target_host": url_host(c["to"]) if kind == TOOL_OUTBOUND_URL else None,
                              "connection_id": c.get("grant"), "scopes": json.dumps(c.get("perms", []))})
        return {"agents": [{"external_id": a["id"], "name": a["label"], "owner_id": a["creator"],
                            "status_raw": "on", "status": "active"} for a in items],
                "agent_tools": tools}

    def _grants(self, items):
        return {"connections": [{"external_id": g["id"], "app": g["service"], "authorized_by": g["by"],
                                 "scopes": json.dumps(g["perms"])} for g in items]}


NIMBUS = {
    "people": [
        {"id": "p-1", "name": "Dana", "mail": "dana@corp.example", "enabled": True, "two_factor": True,
         "seen": "2026-11-08T10:00:00Z"},
        {"id": "p-2", "name": "Lee", "mail": "lee@corp.example", "enabled": False, "two_factor": True,
         "seen": "2026-05-01T10:00:00Z"},
        {"id": "p-3", "name": "Kim", "mail": "kim@corp.example", "enabled": True, "two_factor": False,
         "seen": "2026-08-01T10:00:00Z"},
    ],
    "bots": [{"id": "b-1", "name": "sync-bot"}],
    "teams": [{"id": "t-1", "title": "Owners", "people": ["p-3", "b-1"]}],
    "assistants": [{"id": "x-1", "label": "HR Helper", "creator": "p-2", "capabilities": [
        {"type": "integration", "to": "hris", "grant": "gr-1", "perms": ["read"]},
        {"type": "webhook", "to": "https://collector.outside.example/in"},
    ]}],
    "grants": [{"id": "gr-1", "service": "hris", "by": "p-2", "perms": ["read"]}],
}

LUMEN_LIKE = {  # a tiny first app in the same database, to show the two don't interfere
    "people": [{"id": "p-1", "name": "Dana", "mail": "dana@corp.example", "enabled": True, "two_factor": True,
                "seen": "2026-11-08T10:00:00Z"}],
    "bots": [], "teams": [], "assistants": [], "grants": [],
}


class OtherConnector(NimbusConnector):
    source_app = "other"


class ReuseTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.load_config({"internal_domains": ["corp.example"],
                                "admin_groups": {"nimbus": ["Owners"]},
                                "app_classification": {}})
        run_sync(OtherConnector(LUMEN_LIKE), self.store, "memory://other")
        run_sync(NimbusConnector(NIMBUS), self.store, "memory://nimbus")

    def tearDown(self):
        self.store.close()

    def rows(self, name):
        cursor = self.store.db.execute((QUERIES / name).read_text())
        cols = [d[0] for d in cursor.description]
        return [dict(zip(cols, r)) for r in cursor.fetchall()]

    def test_two_resources_feed_one_table_without_wiping_each_other(self):
        count = lambda: self.store.db.execute(
            "SELECT resource, COUNT(*) FROM identities WHERE source_app = 'nimbus' GROUP BY resource").fetchall()
        before = [tuple(r) for r in count()]
        self.assertEqual(before, [("bots", 1), ("people", 3)])
        run_sync(NimbusConnector(NIMBUS), self.store, "memory://nimbus")   # refresh again
        self.assertEqual([tuple(r) for r in count()], before)

    def test_rows_with_keys_in_any_order_or_missing_land_in_the_right_columns(self):
        rows = {"identities": [
            {"external_id": "z-1", "display_name": "First", "email": "first@corp.example"},
            {"email": "second@corp.example", "display_name": "Second", "external_id": "z-2"},   # other order
            {"external_id": "z-3", "email": "third@corp.example"},                              # key missing
        ]}
        self.store.replace_resource(0, "zeta", "people", ("identities",), [{"id": r["external_id"]} for r in rows["identities"]], rows)
        got = [tuple(r) for r in self.store.db.execute(
            "SELECT external_id, display_name, email FROM identities WHERE source_app = 'zeta' ORDER BY 1")]
        self.assertEqual(got, [("z-1", "First", "first@corp.example"),
                               ("z-2", "Second", "second@corp.example"),
                               ("z-3", None, "third@corp.example")])
        with self.assertRaises(ValueError):                           # a typo'd key fails loudly
            self.store.replace_resource(0, "zeta", "people", ("identities",), [{"id": "z-4"}],
                                        {"identities": [{"external_id": "z-4", "emial": "x"}]})

    def test_apps_share_one_database_without_interfering(self):
        apps = dict(self.store.db.execute("SELECT source_app, COUNT(*) FROM identities GROUP BY source_app"))
        self.assertEqual(apps, {"nimbus": 4, "other": 1})
        dana = self.store.db.execute(
            "SELECT apps, accounts FROM v_person_accounts WHERE person = 'dana@corp.example'").fetchone()
        self.assertEqual(tuple(dana), (2, 2))                           # same person linked across apps

    def test_unchanged_queries_answer_for_the_second_app(self):
        q1 = {r["agent_id"]: r["reason"] for r in self.rows("q1_agents_without_living_owner.sql")}
        self.assertEqual(q1, {"x-1": "owner not active (disabled)"})

        q2 = {r["reaches"]: r for r in self.rows("q2_sensitive_or_external_paths.sql")}
        self.assertEqual(q2["hris"]["why"], "sensitive: hris")          # unclassified app: sensitive
        self.assertEqual(q2["https://collector.outside.example/in"]["why"], "external: collector.outside.example")
        self.assertEqual(q2["hris"]["agent_combines"], "YES")

        q3 = {r["user_id"]: r["reasons"] for r in self.rows("q3_admins_mfa_or_stale.sql")}
        self.assertEqual(q3, {"p-3": "MFA not confirmed; no login in 30 days"})   # the bot is excluded


if __name__ == "__main__":
    unittest.main()
