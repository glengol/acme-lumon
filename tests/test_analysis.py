"""Tests for the analysis layer: the views and the Q1-Q3 queries.

These rules decide the findings, so each one is tested directly on a small
hand-built database (invented data), independent of any API.

Run: python3 -m unittest -v
"""

from __future__ import annotations

import json
import unittest

from lumen_sync.report import QUERIES
from lumen_sync.store import Store

APP = "t"
CONFIG = {
    "internal_domains": ["corp.example"],
    "admin_groups": {APP: ["Admins"]},
    "app_classification": {"wiki": {"sensitive": False}, "payroll": {"sensitive": True}},
}


class AnalysisTest(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.store.load_config(CONFIG)
        self.db = self.store.db

    def tearDown(self):
        self.store.close()

    # ------------------------------------------------------------ helpers

    def user(self, uid, kind="human", status="active", mfa=1, login="2026-11-08T09:00:00Z", email=None):
        self.db.execute(
            "INSERT INTO identities (source_app, resource, external_id, display_name, kind, status_raw, status, "
            "mfa_enabled, last_login_at, email, email_domain) VALUES (?, 'users', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (APP, uid, uid.upper(), kind, status, status.lower() if status else None, mfa, login, email,
             email.split("@")[1] if email else None))

    def group(self, gid, name, *members):
        self.db.execute("INSERT INTO groups (source_app, resource, external_id, name) VALUES (?, 'groups', ?, ?)",
                        (APP, gid, name))
        for member in members:
            kind, mid = member.split(":")
            self.db.execute("INSERT INTO group_members (source_app, resource, group_id, member_type, member_id) "
                            "VALUES (?, 'groups', ?, ?, ?)", (APP, gid, kind, mid))

    def agent(self, aid, owner=None, status="active"):
        self.db.execute("INSERT INTO agents (source_app, resource, external_id, name, owner_id, status_raw, status) "
                        "VALUES (?, 'agents', ?, ?, ?, ?, ?)", (APP, aid, aid, owner, status, status))

    def tool(self, aid, tid, kind, target, connection=None, scopes=(), host=None):
        self.db.execute("INSERT INTO agent_tools (source_app, resource, agent_id, tool_id, kind, target, target_host, "
                        "connection_id, scopes) VALUES (?, 'agents', ?, ?, ?, ?, ?, ?, ?)",
                        (APP, aid, tid, kind, target, host, connection, json.dumps(list(scopes))))

    def connection(self, cid, app, authorized_by, scopes=("read",)):
        self.db.execute("INSERT INTO connections (source_app, resource, external_id, app, authorized_by, scopes) "
                        "VALUES (?, 'connections', ?, ?, ?, ?)", (APP, cid, app, authorized_by, json.dumps(list(scopes))))

    def query(self, name):
        cursor = self.db.execute((QUERIES / name).read_text())
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]

    def path(self, tool_id):
        row = self.db.execute("SELECT is_sensitive, is_external FROM v_agent_paths WHERE tool_id = ?",
                              (tool_id,)).fetchone()
        return tuple(row)

    # ------------------------------------------------------------ admins (v_admins)

    def test_admins_follow_nested_groups_and_survive_cycles(self):
        self.user("u-direct")
        self.user("u-nested")
        self.user("u-deep")
        self.group("g-a", "Admins", "user:u-direct", "group:g-b")
        self.group("g-b", "IT", "user:u-nested", "group:g-c")
        self.group("g-c", "Ops", "user:u-deep", "group:g-a")          # cycle back to the top
        rows = {r[0]: r[1] for r in self.db.execute("SELECT admin_id, admin_via FROM v_admins")}
        self.assertEqual(rows, {"u-direct": "Admins", "u-nested": "Admins > IT", "u-deep": "Admins > IT > Ops"})

    def test_admin_via_is_the_shortest_path(self):
        self.user("u-1")
        self.group("g-a", "Admins", "group:g-z", "user:u-1")          # direct, and also via a nested group
        self.group("g-z", "AAA Team", "user:u-1")
        via = self.db.execute("SELECT admin_via FROM v_admins WHERE admin_id = 'u-1'").fetchone()[0]
        self.assertEqual(via, "Admins")

    # ------------------------------------------------------------ Q3

    def test_q3_flags_each_admin_problem_with_its_reason(self):
        self.user("u-ok")                                              # fine: not listed
        self.user("u-nomfa", mfa=None)
        self.user("u-mfaoff", mfa=0)
        self.user("u-stale", login="2026-10-09T23:59:00Z")             # day before the cut-off
        self.user("u-edge", login="2026-10-10T00:00:00Z")              # exactly 30 days: not stale
        self.user("u-never", login=None)
        self.user("u-baddate", login="10/09/2026 08:00")
        self.user("u-epoch", login="1757491200")
        self.user("u-susp", status="SUSPENDED")
        self.user("u-notype", kind=None)
        self.user("u-svc", kind="service", mfa=None)                   # service: not in Q3
        self.group("g-a", "Admins", *[f"user:{u}" for u in (
            "u-ok", "u-nomfa", "u-mfaoff", "u-stale", "u-edge", "u-never", "u-baddate", "u-epoch",
            "u-susp", "u-notype", "u-svc", "u-ghost")])
        rows = {r["user_id"]: r for r in self.query("q3_admins_mfa_or_stale.sql")}

        self.assertNotIn("u-ok", rows)
        self.assertNotIn("u-edge", rows)
        self.assertNotIn("u-svc", rows)
        expect = {
            "u-nomfa": "MFA not confirmed", "u-mfaoff": "MFA not confirmed",
            "u-stale": "no login in 30 days", "u-never": "no login recorded",
            "u-baddate": "login date unreadable", "u-epoch": "login date unreadable",
            "u-susp": "account not active (SUSPENDED)", "u-notype": "account type unknown",
            "u-ghost": "admin member not found in users",
        }
        for uid, reason in expect.items():
            self.assertIn(uid, rows, uid)
            self.assertIn(reason, rows[uid]["reasons"], uid)
        self.assertEqual(rows["u-nomfa"]["mfa"], "unknown (not reported)")
        self.assertEqual(rows["u-mfaoff"]["mfa"], "off")
        self.assertEqual(rows["u-stale"]["days_since_login"], 31)      # counted by calendar date

    def test_admin_warnings_for_ambiguous_missing_and_unknown_members(self):
        self.user("u-1")
        self.group("g-a", "Admins", "user:u-1", "group:g-missing", "robot:r-1")
        self.group("g-b", "admins ")                                   # same name, different case/space
        warnings = [r[0] for r in self.db.execute("SELECT warning FROM v_admin_warnings")]
        self.assertTrue(any("matches 2 groups" in w for w in warnings), warnings)
        self.assertTrue(any("nested group g-missing" in w and "does not exist" in w for w in warnings), warnings)
        self.assertTrue(any("r-1" in w and 'unknown type "robot"' in w for w in warnings), warnings)

    def test_one_definition_of_human_in_q1_and_q3(self):
        self.user("u-bot", kind="bot")
        self.group("g-a", "Admins", "user:u-bot")
        self.agent("a-1", "u-bot")
        q1 = {r["agent_id"]: r["reason"] for r in self.query("q1_agents_without_living_owner.sql")}
        q3 = {r["user_id"]: r["reasons"] for r in self.query("q3_admins_mfa_or_stale.sql")}
        self.assertEqual(q1["a-1"], "owner is not a human (bot)")
        self.assertIn('account type "bot" is not human', q3["u-bot"])   # flagged, never called human

    # ------------------------------------------------------------ Q1

    def test_q1_living_owner_rules(self):
        self.user("u-alive")
        self.user("u-gone", status="DEPROVISIONED")
        self.user("u-svc", kind="service")
        self.user("u-notype", kind=None)
        for aid, owner in [("a-ok", "u-alive"), ("a-gone", "u-gone"), ("a-svc", "u-svc"),
                           ("a-notype", "u-notype"), ("a-none", None), ("a-ghost", "u-ghost")]:
            self.agent(aid, owner)
        reasons = {r["agent_id"]: r["reason"] for r in self.query("q1_agents_without_living_owner.sql")}
        self.assertEqual(reasons, {
            "a-gone": "owner not active (DEPROVISIONED)",
            "a-svc": "owner is not a human (service)",
            "a-notype": "owner kind unknown",
            "a-none": "no owner set",
            "a-ghost": "owner id not found in users",
        })

    # ------------------------------------------------------------ sensitive / external (v_agent_paths, Q2)

    def test_sensitive_rules_if_in_doubt(self):
        self.agent("a-1")
        self.tool("a-1", "t-cleared", "app_connection", "wiki", scopes=["read"])
        self.tool("a-1", "t-cleared-write", "app_connection", "wiki", scopes=["pages:Write"])
        self.tool("a-1", "t-unlisted", "app_connection", "crm", scopes=["read"])
        self.tool("a-1", "t-listed", "app_connection", "payroll", scopes=["read"])
        self.tool("a-1", "t-nokind", None, "crm", scopes=["read"])
        self.tool("a-1", "t-name-only", "app_connection", "wiki", scopes=["read"])
        self.db.execute("UPDATE agents SET name = 'Wiki write-ups' WHERE external_id = 'a-1'")  # name is not a scope
        self.assertEqual(self.path("t-cleared"), (0, 0))
        self.assertEqual(self.path("t-cleared-write"), (1, 0))       # write scope beats the safe list
        self.assertEqual(self.path("t-unlisted"), (1, 0))            # not classified: sensitive
        self.assertEqual(self.path("t-listed"), (1, 0))
        self.assertEqual(self.path("t-nokind"), (1, 0))              # unknown kind is not dropped
        self.assertEqual(self.path("t-name-only"), (0, 0))

    def test_powerful_scopes_make_any_app_sensitive(self):
        self.agent("a-1")
        for i, scope in enumerate(["*", "issues:delete", "project.manage", "full_access", "owner", "read"]):
            self.tool("a-1", f"t-{i}", "app_connection", "wiki", scopes=[scope])
        flags = [self.path(f"t-{i}")[0] for i in range(6)]
        self.assertEqual(flags, [1, 1, 1, 1, 1, 0])

    def test_connection_app_disagreeing_with_tool_target(self):
        self.user("u-1")
        self.agent("a-1", "u-1")
        self.connection("c-1", "wiki", "u-1")                            # connection claims a cleared app
        self.tool("a-1", "t-1", "app_connection", "payroll", connection="c-1", scopes=["read"])
        self.connection("c-2", "payroll", "u-1")                         # and the other way round
        self.tool("a-1", "t-2", "app_connection", "wiki", connection="c-2", scopes=["read"])
        self.assertEqual(self.path("t-1"), (1, 0))
        self.assertEqual(self.path("t-2"), (1, 0))                       # either side sensitive is enough
        why = {r["reaches"]: r["why"] for r in self.query("q2_sensitive_or_external_paths.sql")}
        self.assertEqual(why["payroll"], "sensitive: payroll (connection says wiki)")
        self.assertEqual(why["wiki"], "sensitive: wiki (connection says payroll)")

    def test_external_rules_use_exact_domain_suffix(self):
        self.agent("a-1")
        cases = {
            "t-same": ("corp.example", 0),
            "t-sub": ("hooks.corp.example", 0),
            "t-lookalike": ("evilcorp.example", 1),                  # not a subdomain
            "t-underscore": ("hooks.corp_example", 1),               # '_' must not act as a wildcard
            "t-other": ("hooks.vendor.example", 1),
            "t-unparsed": (None, 1),                                 # unparseable URL: treat as external
        }
        for tid, (host, _) in cases.items():
            self.tool("a-1", tid, "outbound_url", f"https://{host}/x", host=host)
        for tid, (_, external) in cases.items():
            self.assertEqual(self.path(tid)[1], external, tid)

    def test_q2_authorizer_and_combination(self):
        self.user("u-owner")
        self.user("u-contractor", email="c@partner.example")
        self.agent("a-leak", "u-owner")
        self.connection("c-1", "payroll", "u-contractor")
        self.tool("a-leak", "t-pay", "app_connection", "payroll", connection="c-1", scopes=["read"])
        self.tool("a-leak", "t-hook", "outbound_url", "https://x.vendor.example/h", host="x.vendor.example")
        self.agent("a-broken", "u-owner")
        self.tool("a-broken", "t-missing", "app_connection", "payroll", connection="c-nope")
        rows = {r["agent_id"] + "/" + r["reaches"]: r for r in self.query("q2_sensitive_or_external_paths.sql")}

        pay = rows["a-leak/payroll"]
        self.assertIn("U-CONTRACTOR (u-contractor, active, external: partner.example)", pay["authorized_by"])
        self.assertEqual(pay["agent_combines"], "YES")
        self.assertEqual(rows["a-leak/https://x.vendor.example/h"]["authorized_by"],
                         "none on record (no stored connection)")   # never filled in with the owner
        self.assertEqual(rows["a-broken/payroll"]["authorized_by"], "unknown (connection c-nope not found)")
        self.assertEqual(rows["a-broken/payroll"]["agent_combines"], "")


if __name__ == "__main__":
    unittest.main()
