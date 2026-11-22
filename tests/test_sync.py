"""End-to-end tests against a small fake API (invented data, not the mock's).

The fake reproduces the behaviours observed while probing Lumen:
- pages past `total_pages` repeat the last page
- /v1/audit returns 403 insufficient_scope
- a switch to make every endpoint fail, to test that old data survives

Run: python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from lumen_sync.connectors.lumen import LumenConnector
from lumen_sync.http import HttpClient
from lumen_sync.runner import run_sync
from lumen_sync.store import Store

TOKEN = "test-token"
USERS = [{"id": f"u-{i}", "display_name": f"User {i}", "email": f"user{i}@example.org", "type": "human",
          "status": "ACTIVE" if i % 2 else "active", "last_login_at": "2026-11-01T00:00:00Z"} for i in range(5)]
AGENTS = [{"id": "a-1", "name": "Bot", "owner_id": "u-0", "status": "active",
           "tools": [{"id": "t-1", "kind": "http", "target": "https://Hooks.Example.net/x",
                      "connection_id": None, "scopes": []}]}]
STATE = {"fail_all": False}


class FakeApi(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        url = urlparse(self.path)
        if STATE["fail_all"]:
            return self._send(500, {"error": "internal"})
        if self.headers.get("Authorization") != f"Bearer {TOKEN}":
            return self._send(401, {"error": "unauthorized"})
        page = int(parse_qs(url.query).get("page", ["1"])[0])
        if url.path == "/v1/users":
            return self._send(200, self._page(USERS, page))
        if url.path == "/v1/agents":
            return self._send(200, self._page(AGENTS, page))
        if url.path == "/v1/groups":
            return self._send(200, {"data": [{"id": "g-1", "name": "Admins",
                                              "members": [{"type": "user", "id": "u-1"}]}]})
        if url.path == "/v1/connections":
            return self._send(200, {"data": []})
        if url.path == "/v1/audit":
            return self._send(403, {"error": "insufficient_scope", "required_scopes": ["audit.read"]})
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
        STATE["fail_all"] = False
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

    def test_wrong_token_is_not_retried_and_is_explained(self):
        cov = self.coverage(self.sync(token="nope"))
        self.assertEqual(cov["users"]["http_status"], 401)
        self.assertIn("token", cov["users"]["detail"])

    def test_normalization(self):
        self.sync()
        statuses = {r[0] for r in self.store.db.execute("SELECT status FROM identities")}
        self.assertEqual(statuses, {"active"})
        mfa = {r[0] for r in self.store.db.execute("SELECT mfa_enabled FROM identities")}
        self.assertEqual(mfa, {None})  # field absent -> unknown, not false
        host = self.store.db.execute("SELECT target_host FROM agent_tools").fetchone()[0]
        self.assertEqual(host, "hooks.example.net")


if __name__ == "__main__":
    unittest.main()
