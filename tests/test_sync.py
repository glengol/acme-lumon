"""End-to-end tests against a small fake API (invented data, not the mock's).

The fake reproduces the behaviours observed while probing Lumen:
- pages past `total_pages` repeat the last page
- /v1/audit returns 403 insufficient_scope
- a switch to make every endpoint fail, to test that old data survives
- a switch to grant audit access, with events that can age out (retention)

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from lumen_sync.connectors.lumen import LumenConnector
from lumen_sync.report import QUERIES
from lumen_sync.http import ApiError, HttpClient
from lumen_sync.runner import run_sync
from lumen_sync.store import Store

TOKEN = "test-token"
USERS = [{"id": f"u-{i}", "display_name": f"User {i}", "email": f"user{i}@example.org", "type": "human",
          "status": "ACTIVE" if i % 2 else "active", "last_login_at": "2026-11-01T00:00:00Z"} for i in range(5)]
AGENTS = [{"id": "a-1", "name": "Bot", "owner_id": "u-0", "status": "active",
           "tools": [{"id": "t-1", "kind": "http", "target": "https://Hooks.Example.net/x",
                      "connection_id": None, "scopes": []}]}]
STATE = {"fail_all": False, "audit_allowed": False, "events": [], "since_seen": [],
         "audit_paged": False, "grow_after_first_page": [], "shrink_after_first_page": 0, "users": None}


def event(n, time):
    return {"id": f"e-{n}", "time": time, "action": "agent.tool.invoked",
            "actor_id": "a-1", "target_id": "c-1", "detail": "x"}


class FakeApi(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        if STATE["fail_all"]:
            return self._send(500, {"error": "internal"})
        if url.path == "/redirect":                       # would carry the token to another host
            self.send_response(302)
            self.send_header("Location", "http://other-host.example/steal")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if url.path == "/busy":                           # 429 once, with Retry-After, then OK
            STATE["busy_calls"] = STATE.get("busy_calls", 0) + 1
            if STATE["busy_calls"] == 1:
                self.send_response(429)
                self.send_header("Retry-After", "0.2")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
                return
            return self._send(200, {"data": []})
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._send(401, {"error": "unauthorized"})
        page = int(parse_qs(url.query).get("page", ["1"])[0])
        if url.path == "/v1/users":
            users = USERS if STATE["users"] is None else STATE["users"]
            return self._send(200, self._page(users, page) if users else {"data": [], "page": 1, "total_pages": 1})
        if url.path == "/v1/agents":
            return self._send(200, self._page(AGENTS, page))
        if url.path == "/v1/groups":
            return self._send(200, {"data": [{"id": "g-1", "name": "Admins",
                                              "members": [{"type": "user", "id": "u-1"}]}]})
        if url.path == "/v1/connections":
            return self._send(200, {"data": []})
        if url.path == "/v1/audit":
            if not STATE["audit_allowed"]:
                return self._send(403, {"error": "insufficient_scope", "required_scopes": ["audit.read"]})
            since = parse_qs(url.query).get("since", [None])[0]
            if page == 1:
                STATE["since_seen"].append(since)
            events = [e for e in STATE["events"] if since is None or e["time"] >= since]
            if not STATE["audit_paged"]:
                return self._send(200, {"data": events})
            body = self._page(events, page)
            if page == 1 and STATE["grow_after_first_page"]:   # a live log grows while we read it
                STATE["events"] += STATE.pop("grow_after_first_page")
                STATE["grow_after_first_page"] = []
            if page == 1 and STATE["shrink_after_first_page"]:  # retention drops the oldest events mid-read
                STATE["events"] = STATE["events"][STATE["shrink_after_first_page"]:]
                STATE["shrink_after_first_page"] = 0
            return self._send(200, body)
        return self._send(404, {"error": "not_found"})

    @staticmethod
    def _page(items, page, size=2):
        total = (len(items) + size - 1) // size
        index = min(page, total)  # past the end: repeat the last page, like Lumen
        return {"data": items[(index - 1) * size: index * size], "page": page, "total_pages": total}

    def _send(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


class SyncTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeApi)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        STATE.update(fail_all=False, audit_allowed=False, events=[], since_seen=[],
                     audit_paged=False, grow_after_first_page=[], shrink_after_first_page=0, users=None)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(str(Path(self.tmp.name) / "t.db"))

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def sync(self, token=TOKEN):
        client = HttpClient(self.base_url, token, retries=1, backoff=0)
        return run_sync(LumenConnector(client), self.store, self.base_url)

    def snapshot(self):
        tables = ["identities", "groups", "group_members", "agents", "agent_tools", "connections", "events"]
        return {t: self.store.db.execute(f"SELECT * FROM {t} ORDER BY 1, 2, 3").fetchall() for t in tables}

    def coverage(self, run_id):
        return {r["resource"]: dict(r) for r in
                self.store.db.execute("SELECT * FROM coverage WHERE run_id = ?", (run_id,))}

    def test_collects_every_page_once(self):
        cov = self.coverage(self.sync())
        self.assertEqual(cov["users"]["records"], 5)
        self.assertEqual(cov["users"]["pages"], 3)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM identities").fetchone()[0], 5)

    def test_second_run_changes_nothing(self):
        self.sync()
        first = [[tuple(r) for r in rows] for rows in self.snapshot().values()]
        self.sync()
        second = [[tuple(r) for r in rows] for rows in self.snapshot().values()]
        self.assertEqual(first, second)

    def test_forbidden_resource_is_reported_and_others_still_load(self):
        run_id = self.sync()
        cov = self.coverage(run_id)
        self.assertEqual(cov["audit"]["status"], "failed")
        self.assertEqual(cov["audit"]["http_status"], 403)
        self.assertIn("audit.read", cov["audit"]["detail"])
        self.assertEqual({k for k, v in cov.items() if v["status"] == "ok"},
                         {"users", "groups", "agents", "connections"})
        outcome = self.store.db.execute("SELECT outcome FROM sync_runs WHERE run_id = ?", (run_id,)).fetchone()[0]
        self.assertEqual(outcome, "partial")

    def test_failed_run_keeps_previous_data(self):
        self.sync()
        before = self.snapshot()
        STATE["fail_all"] = True
        run_id = self.sync()
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(all(v["status"] == "failed" for v in self.coverage(run_id).values()))

    def test_wrong_token_is_explained_and_stops_the_run(self):
        cov = self.coverage(self.sync(token="nope"))
        self.assertEqual(cov["users"]["http_status"], 401)
        self.assertIn("token", cov["users"]["detail"])
        for name in ("groups", "agents", "connections", "audit"):   # not attempted after the 401
            self.assertEqual(cov[name]["error"], "skipped")
            self.assertIn("token rejected (401) on users", cov[name]["detail"])

    def test_redirect_is_refused_so_the_token_stays_put(self):
        client = HttpClient(self.base_url, TOKEN, retries=0)
        with self.assertRaises(ApiError) as ctx:
            client.get_json("/redirect")
        self.assertEqual(ctx.exception.http_status, 302)
        self.assertIn("redirect_refused", ctx.exception.error)

    def test_retry_after_is_honoured(self):
        STATE["busy_calls"] = 0
        client = HttpClient(self.base_url, TOKEN, retries=1, backoff=30)   # backoff would take 30 s
        started = time.monotonic()
        self.assertEqual(client.get_json("/busy"), {"data": []})
        self.assertLess(time.monotonic() - started, 5)                     # waited Retry-After (0.2 s) instead
        self.assertEqual(STATE["busy_calls"], 2)

    def event_ids(self):
        return [r[0] for r in self.store.db.execute("SELECT external_id FROM events ORDER BY 1")]

    def test_audit_is_incremental_and_keeps_events_lumen_has_dropped(self):
        STATE["audit_allowed"] = True
        STATE["events"] = [event(1, "2026-11-01T10:00:00Z"), event(2, "2026-11-02T10:00:00Z")]
        first = self.coverage(self.sync())
        self.assertEqual(first["audit"]["status"], "ok")
        self.assertEqual(STATE["since_seen"], ["1970-01-01T00:00:00Z"])  # first run: explicit full history

        # Lumen's retention drops e-1, and a new event e-3 arrives.
        STATE["events"] = [event(2, "2026-11-02T10:00:00Z"), event(3, "2026-11-03T10:00:00Z")]
        cov = self.coverage(self.sync())
        self.assertEqual(STATE["since_seen"][-1], "2026-11-01T10:00:00Z")  # newest stored minus 24 h
        self.assertEqual(self.event_ids(), ["e-1", "e-2", "e-3"])        # e-1 kept, e-2 not duplicated
        self.assertEqual(cov["audit"]["records"], 1)                      # only e-3 counted as new
        self.assertIn("1 new, 1 already stored", cov["audit"]["detail"])

        self.sync()                                                       # nothing new: still exactly 3
        self.assertEqual(len(self.event_ids()), 3)

    def test_audit_picks_up_events_logged_late(self):
        STATE["audit_allowed"] = True
        STATE["events"] = [event(1, "2026-11-03T10:00:00Z")]
        self.sync()
        # e-2 shows up later but is stamped 20 hours before the newest stored event.
        STATE["events"].append(event(2, "2026-11-02T14:00:00Z"))
        self.sync()
        self.assertEqual(self.event_ids(), ["e-1", "e-2"])

    def test_audit_tolerates_a_log_that_grows_while_paging(self):
        STATE.update(audit_allowed=True, audit_paged=True)
        STATE["events"] = [event(i, f"2026-11-0{i}T10:00:00Z") for i in range(1, 5)]   # 2 pages
        STATE["grow_after_first_page"] = [event(5, "2026-11-05T10:00:00Z"),
                                          event(6, "2026-11-06T10:00:00Z")]             # becomes 3 pages
        cov = self.coverage(self.sync())
        self.assertEqual(cov["audit"]["status"], "ok")
        self.assertEqual(self.event_ids(), [f"e-{i}" for i in range(1, 7)])

    def test_audit_survives_a_log_that_shrinks_while_paging(self):
        STATE.update(audit_allowed=True, audit_paged=True)
        STATE["events"] = [event(i, f"2026-11-0{i}T10:00:00Z") for i in range(1, 8)]   # 4 pages of 2
        STATE["shrink_after_first_page"] = 2       # e-1, e-2 dropped after page 1: the rest shift back a page
        self.sync()
        self.assertEqual(self.event_ids(), [f"e-{i}" for i in range(1, 8)])   # nothing skipped

    def test_audit_cursor_ignores_future_and_unreadable_times(self):
        STATE["audit_allowed"] = True
        STATE["events"] = [event(1, "2026-11-03T10:00:00Z")]
        self.sync()
        db = self.store.db
        db.execute("INSERT INTO events (source_app, resource, external_id, occurred_at) "
                   "VALUES ('lumen', 'audit', 'e-skew', '2099-01-01T00:00:00Z')")  # clock skew
        db.execute("INSERT INTO events (source_app, resource, external_id, occurred_at) "
                   "VALUES ('lumen', 'audit', 'e-bad', 'yesterday-ish')")         # unreadable
        db.commit()
        STATE["events"].append(event(2, "2026-11-04T10:00:00Z"))
        cov = self.coverage(self.sync())
        self.assertEqual(cov["audit"]["status"], "ok")
        self.assertEqual(STATE["since_seen"][-1], "2026-11-02T10:00:00Z")   # from the real newest event
        self.assertIn("e-2", self.event_ids())

    def test_empty_snapshot_does_not_wipe_stored_data(self):
        self.sync()
        STATE["users"] = []                                   # API suddenly returns no users
        cov = self.coverage(self.sync())
        self.assertEqual(cov["users"]["status"], "failed")
        self.assertEqual(cov["users"]["error"], "empty_response_refused")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM identities").fetchone()[0], 5)
        client = HttpClient(self.base_url, TOKEN, retries=1, backoff=0)
        run_sync(LumenConnector(client), self.store, self.base_url, allow_empty=True)   # explicit override
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM identities").fetchone()[0], 0)

    def test_a_malformed_field_fails_one_resource_not_the_run(self):
        self.sync()
        STATE["users"] = [dict(USERS[0], display_name={"first": "A"})]   # wrong type for a text column
        run_id = self.sync()
        cov = self.coverage(run_id)
        self.assertEqual(cov["users"]["status"], "failed")
        self.assertIn("store_error", cov["users"]["error"])
        self.assertEqual(cov["agents"]["status"], "ok")                  # the rest still ran
        outcome = self.store.db.execute("SELECT outcome, finished_at FROM sync_runs WHERE run_id = ?",
                                        (run_id,)).fetchone()
        self.assertEqual(outcome[0], "partial")
        self.assertIsNotNone(outcome[1])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM identities").fetchone()[0], 5)

    def test_queries_never_blank_out_when_fields_are_missing(self):
        """In SQL, 'x' || NULL is NULL. One missing field must not empty a whole answer cell."""
        self.sync()
        db = self.store.db
        db.execute("INSERT INTO connections (source_app, resource, external_id, app, authorized_by, scopes) "
                   "VALUES ('lumen', 'connections', 'c-1', 'payroll', 'u-2', '[]')")
        db.execute("INSERT INTO agent_tools (source_app, resource, agent_id, tool_id, kind, target, connection_id, scopes) "
                   "VALUES ('lumen', 'agents', 'a-1', 't-2', 'app_connection', 'payroll', 'c-1', '[]')")
        db.execute("UPDATE identities SET status = NULL, status_raw = NULL WHERE external_id = 'u-2'")
        db.execute("UPDATE agents SET owner_id = 'u-missing' WHERE external_id = 'a-1'")
        for name in ("q1_agents_without_living_owner.sql", "q2_sensitive_or_external_paths.sql"):
            cursor = db.execute((QUERIES / name).read_text())
            columns = [d[0] for d in cursor.description]
            rows = cursor.fetchall()
            self.assertTrue(rows, name)
            for row in rows:
                for column in ("owner", "reason", "authorized_by", "why"):
                    if column in columns:
                        self.assertTrue(row[columns.index(column)], f"{name}: empty {column} in {tuple(row)}")

    def test_admin_group_matches_by_id_or_loose_name_and_warns_when_missing(self):
        self.sync()                                   # fake API has group g-1 "Admins" with member u-1
        admins = lambda: [r[0] for r in self.store.db.execute("SELECT external_id FROM v_admins")]
        missing = lambda: [r[0] for r in self.store.db.execute("SELECT group_name FROM v_admin_groups_missing")]
        for entry in (" admins ", "ADMINS", "g-1"):
            self.store.load_config({"admin_groups": {"lumen": [entry]}})
            self.assertEqual(admins(), ["u-1"], entry)
            self.assertEqual(missing(), [], entry)
        self.store.load_config({"admin_groups": {"lumen": ["Admin Team"]}})
        self.assertEqual(admins(), [])
        self.assertEqual(missing(), ["Admin Team"])   # surfaced as a warning, not silent

    def test_normalization(self):
        self.sync()
        statuses = {r[0] for r in self.store.db.execute("SELECT status FROM identities")}
        self.assertEqual(statuses, {"active"})
        mfa = {r[0] for r in self.store.db.execute("SELECT mfa_enabled FROM identities")}
        self.assertEqual(mfa, {None})  # field absent -> unknown, not false
        row = self.store.db.execute("SELECT kind, kind_raw, target_host FROM agent_tools").fetchone()
        self.assertEqual(tuple(row), ("outbound_url", "http", "hooks.example.net"))  # Lumen word -> neutral


if __name__ == "__main__":
    unittest.main()
