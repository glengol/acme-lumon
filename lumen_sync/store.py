"""SQLite storage. App-neutral: works for any connector."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = Path(__file__).with_name("schema.sql")
SCHEMA_VERSION = 2  # bump when schema.sql changes shape; there are no migrations (see submission.md)
VIEWS = Path(__file__).with_name("views.sql")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")  # make the REFERENCES in schema.sql real
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        has_tables = self.db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'identities'").fetchone()
        if has_tables and version != SCHEMA_VERSION:
            self.db.close()
            raise SystemExit(f"error: {path} was created by an older version (schema {version}, need "
                             f"{SCHEMA_VERSION}). Delete it or pass a new --db, then run sync.")
        self.db.executescript(SCHEMA.read_text())
        self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
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
                # Only this resource's rows: another resource may feed the same table.
                self.db.execute(f"DELETE FROM {table} WHERE source_app = ? AND resource = ?", (source_app, resource))
                table_rows = rows.get(table, [])
                if not table_rows:
                    continue
                columns = ["source_app", "resource", *table_rows[0].keys()]
                sql = f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join('?' * len(columns))})"
                self.db.executemany(sql, [(source_app, resource, *r.values()) for r in table_rows])

    def append_resource(self, run_id: int, source_app: str, resource: str, tables: tuple[str, ...],
                        raw_items: list[dict], rows: dict[str, list[dict]]) -> int:
        """Add or update rows by primary key, never delete. For logs such as audit events.

        Re-fetching an overlap window is harmless: the same event id lands on the same row.
        Returns how many records were new (not already stored).
        """
        known = {r[0] for r in self.db.execute(
            "SELECT external_id FROM raw_records WHERE source_app = ? AND resource = ?", (source_app, resource))}
        new_count = sum(1 for item in raw_items if item["id"] not in known)
        with self.db:
            self.db.executemany(
                "INSERT OR REPLACE INTO raw_records (source_app, resource, external_id, payload, run_id) "
                "VALUES (?, ?, ?, ?, ?)",
                [(source_app, resource, item["id"], json.dumps(item, sort_keys=True), run_id) for item in raw_items])
            for table in tables:
                table_rows = rows.get(table, [])
                if not table_rows:
                    continue
                columns = ["source_app", "resource", *table_rows[0].keys()]
                sql = (f"INSERT OR REPLACE INTO {table} ({', '.join(columns)}) "
                       f"VALUES ({', '.join('?' * len(columns))})")
                self.db.executemany(sql, [(source_app, resource, *r.values()) for r in table_rows])
        return new_count

    def newest(self, source_app: str, table: str, column: str) -> str | None:
        """Newest stored timestamp in `column` for this app, used as an incremental-fetch cursor.

        Values that are not ISO-8601 timestamps, or that lie more than a day in
        the future (a skewed clock), are ignored: one bad event must not move
        the cursor past every real event, or break every later run.
        """
        row = self.db.execute(
            f"SELECT MAX({column}) FROM {table} WHERE source_app = ? "
            f"AND {column} GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]*' "
            f"AND julianday(substr({column}, 1, 19)) IS NOT NULL "
            f"AND julianday(substr({column}, 1, 19)) <= julianday('now', '+1 day')",
            (source_app,)).fetchone()
        return row[0] if row else None

    def count_rows(self, source_app: str, resource: str) -> int:
        """How many objects of this resource are currently stored."""
        return self.db.execute("SELECT COUNT(*) FROM raw_records WHERE source_app = ? AND resource = ?",
                               (source_app, resource)).fetchone()[0]

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
