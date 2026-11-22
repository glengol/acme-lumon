"""Runs any connector into the store, one resource at a time, and reports coverage."""

from __future__ import annotations

import json

from .connectors.base import Connector
from .http import ApiError
from .store import Store

# Plain-language explanation of failures, shown in the coverage report.
EXPLAIN = {
    401: "token missing, wrong or expired",
    403: "token lacks the permission for this endpoint",
    404: "endpoint not found (wrong base URL or API version?)",
    429: "rate limited, still failing after retries",
}


def run_sync(connector: Connector, store: Store, base_url: str) -> int:
    run_id = store.start_run(connector.source_app, base_url)
    ok = failed = 0
    for res in connector.resources():
        cursor = store.newest(connector.source_app, *res.cursor) if res.cursor else None
        try:
            result = res.fetch(cursor)
            rows = res.to_rows(result.items)
        except ApiError as exc:
            failed += 1
            store.record_coverage(run_id, res.name, "failed", http_status=exc.http_status,
                                  error=exc.error, detail=_detail(exc))
            continue
        except Exception as exc:  # malformed object, unexpected shape: keep the run going
            failed += 1
            store.record_coverage(run_id, res.name, "failed", error=f"{type(exc).__name__}: {exc}")
            continue
        write = store.append_resource if res.mode == "append" else store.replace_resource
        write(run_id, connector.source_app, res.name, res.tables, result.items, rows)
        store.record_coverage(run_id, res.name, "ok", records=len(result.items), pages=result.pages,
                              detail="; ".join(result.notes) or None)
        ok += 1

    outcome = "complete" if not failed else ("failed" if not ok else "partial")
    store.finish_run(run_id, outcome)
    return run_id


def _detail(exc: ApiError) -> str | None:
    parts = []
    if exc.http_status in EXPLAIN:
        parts.append(EXPLAIN[exc.http_status])
    elif exc.http_status and exc.http_status >= 500:
        parts.append("server error, still failing after retries")
    if exc.body.get("required_scopes"):
        parts.append("required scopes: " + ", ".join(exc.body["required_scopes"]))
    extra = {k: v for k, v in exc.body.items() if k not in ("error", "required_scopes")}
    if extra:
        parts.append("server said: " + json.dumps(extra))
    return "; ".join(parts) or None


def print_coverage(store: Store, run_id: int) -> None:
    run = store.db.execute("SELECT * FROM sync_runs WHERE run_id = ?", (run_id,)).fetchone()
    rows = store.db.execute("SELECT * FROM coverage WHERE run_id = ? ORDER BY rowid", (run_id,)).fetchall()
    print(f"\nCoverage report: run {run_id}, {run['source_app']} at {run['base_url']}, outcome: {run['outcome'].upper()}")
    print("-" * 78)
    for r in rows:
        if r["status"] == "ok":
            pages = f", {r['pages']} pages" if r["pages"] else ""
            note = f"  ({r['detail']})" if r["detail"] else ""
            print(f"  [ok]      {r['resource']:<12} {r['records']:>4} records{pages}{note}")
        else:
            status = f"HTTP {r['http_status']} " if r["http_status"] else ""
            print(f"  [MISSING] {r['resource']:<12} {status}{r['error']}")
            if r["detail"]:
                print(f"            {'':<12} {r['detail']}")
    missing = [r["resource"] for r in rows if r["status"] != "ok"]
    if missing:
        print("-" * 78)
        print("  Not collected this run: " + ", ".join(missing) + ".")
        print("  Data from an earlier successful run (if any) is kept for these and may be out of date.")
