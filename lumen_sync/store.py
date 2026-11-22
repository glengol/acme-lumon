"""SQLite storage. App-neutral: works for any connector."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")
VIEWS = Path(__file__).with_name("views.sql")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA.read_text())
        self.db.executescript(VIEWS.read_text())

    # ------------------------------------------------------------ runs and coverage

    def start_run(self, source_app: str, base_url: str) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO sync_runs (source_app, base_url, started_at) VALUES (?, ?, ?)",
                (source_app, base_url, utc_now()))
        return cur.lastrowid

    def finish_run(self, run_id: int, outcome: str) -> None:
        with self.db:
            self.db.execute("UPDATE sync_runs SET finished_at = ?, outcome = ? WHERE run_id = ?",
                            (utc_now(), outcome, run_id))

    def record_coverage(self, run_id: int, resource: str, status: str, records: int = 0,
                        pages: int | None = None, http_status: int | None = None,
                        error: str | None = None, detail: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO coverage (run_id, resource, status, http_status, error, detail, records, pages) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, resource, status, http_status, error, detail, records, pages))

    # ------------------------------------------------------------ data

    def replace_resource(self, run_id: int, source_app: str, resource: str, tables: tuple[str, ...],
                         raw_items: list[dict], rows: dict[str, list[dict]]) -> None:
        """Replace everything one resource owns, atomically.

        Delete-then-insert inside a single transaction: re-running never
        duplicates, objects deleted at the source disappear here too, and a
        crash mid-way rolls back to the previous good copy.
        """
        with self.db:
            self.db.execute("DELETE FROM raw_records WHERE source_app = ? AND resource = ?", (source_app, resource))
            self.db.executemany(
                "INSERT INTO raw_records (source_app, resource, external_id, payload, run_id) VALUES (?, ?, ?, ?, ?)",
                [(source_app, resource, item["id"], json.dumps(item, sort_keys=True), run_id) for item in raw_items])
            for table in tables:
                self.db.execute(f"DELETE FROM {table} WHERE source_app = ?", (source_app,))
                table_rows = rows.get(table, [])
                if not table_rows:
                    continue
                columns = ["source_app", *table_rows[0].keys()]
                sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"
                self.db.executemany(sql, [(source_app, *r.values()) for r in table_rows])

    # ------------------------------------------------------------ policy config

    def load_config(self, config: dict) -> None:
        """Replace the policy tables with the contents of config.json."""
        with self.db:
            self.db.execute("DELETE FROM internal_domains")
            self.db.executemany("INSERT INTO internal_domains (domain) VALUES (?)",
                                [(d.lower(),) for d in config.get("internal_domains", [])])
            self.db.execute("DELETE FROM admin_groups")
            self.db.executemany("INSERT INTO admin_groups (source_app, group_name) VALUES (?, ?)",
                                [(app, name) for app, names in config.get("admin_groups", {}).items() for name in names])
            self.db.execute("DELETE FROM app_classification")
            self.db.executemany("INSERT INTO app_classification (app, sensitive, reason) VALUES (?, ?, ?)",
                                [(app.lower(), int(bool(v["sensitive"])), v.get("reason"))
                                 for app, v in config.get("app_classification", {}).items()])

    def close(self) -> None:
        self.db.close()
