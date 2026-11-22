"""Run the question queries in queries/ and print their results as tables."""

from __future__ import annotations

from pathlib import Path

from .store import Store

QUERIES = Path(__file__).resolve().parent.parent / "queries"


def print_report(store: Store) -> None:
    if store.db.execute("SELECT 1 FROM sync_runs LIMIT 1").fetchone() is None:
        print("No sync has run yet. Run: python3 -m lumen_sync sync")
        return
    _print_freshness(store)

    for path in sorted(QUERIES.glob("q*.sql")):
        sql = path.read_text()
        title = _title(sql) or path.stem
        cursor = store.db.execute(sql)
        columns = [d[0] for d in cursor.description]
        rows = [["" if v is None else str(v) for v in row] for row in cursor.fetchall()]
        print(f"\n{title}\n({path.name}, {len(rows)} row{'s' if len(rows) != 1 else ''})")
        print(_table(columns, rows))


def _print_freshness(store: Store) -> None:
    """Per resource: when its data was last collected, and whether the latest attempt failed.

    The data in the tables comes from each resource's last *successful* run,
    which is not necessarily the latest run, so the answers say which is which.
    """
    rows = store.db.execute("""
        WITH latest AS (
            SELECT c.resource, c.status, c.http_status, c.error, r.started_at,
                   ROW_NUMBER() OVER (PARTITION BY c.resource ORDER BY c.run_id DESC) AS n
            FROM coverage c JOIN sync_runs r USING (run_id)),
        last_ok AS (
            SELECT c.resource, MAX(r.started_at) AS started_at
            FROM coverage c JOIN sync_runs r USING (run_id)
            WHERE c.status = 'ok' GROUP BY c.resource)
        SELECT l.resource, l.status, l.http_status, l.error, l.started_at AS attempted_at,
               o.started_at AS collected_at
        FROM latest l LEFT JOIN last_ok o USING (resource)
        WHERE l.n = 1 ORDER BY l.resource""").fetchall()
    print("Data freshness (answers below use the last successful collection of each resource):")
    for r in rows:
        if r["status"] == "ok":
            print(f"  {r['resource']:<12} collected {r['collected_at']}")
            continue
        why = f"{r['http_status'] or ''} {r['error']}".strip()
        if r["collected_at"]:
            print(f"  {r['resource']:<12} STALE: collected {r['collected_at']}; latest attempt "
                  f"{r['attempted_at']} failed ({why})")
        else:
            print(f"  {r['resource']:<12} NEVER COLLECTED ({why}); no answer can use it")


def _title(sql: str) -> str:
    """The '-- Qn. ...' comment, including its indented continuation lines."""
    parts, inside = [], False
    for line in sql.splitlines():
        if line.startswith("-- Q"):
            inside = True
            parts.append(line[3:].strip())
        elif inside and line.startswith("--    ") and line[3:].strip():
            parts.append(line[3:].strip())
        elif inside:
            break
    return " ".join(parts)


def _table(columns: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(c), *(len(r[i]) for r in rows)) if rows else len(c) for i, c in enumerate(columns)]
    line = lambda cells: "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"
    out = [line(columns), "|" + "|".join("-" * (w + 2) for w in widths) + "|"]
    out += [line(r) for r in rows] or ["| (no rows) |"]
    return "\n".join(out)
