"""Command line entry point.

    python3 -m lumen_sync sync      pull from Lumen into SQLite, print coverage
    python3 -m lumen_sync report    answer the questions from the database
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .connectors.lumen import LumenConnector
from .http import HttpClient
from .report import print_report
from .runner import print_coverage, run_sync
from .store import Store

DEFAULT_CONFIG = Path(__file__).with_name("config.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lumen_sync")
    parser.add_argument("command", choices=["sync", "report"])
    parser.add_argument("--db", default=os.environ.get("LUMEN_DB", "lumen.db"), help="SQLite file (default: lumen.db)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="policy config JSON")
    args = parser.parse_args(argv)

    if args.command == "report":
        if not Path(args.db).is_file():
            print(f"error: database {args.db} not found. Run 'sync' first, or pass --db", file=sys.stderr)
            return 2
        store = Store(args.db)
        try:
            store.load_config(json.loads(Path(args.config).read_text()))  # policy edits apply without a re-sync
            print_report(store)
        finally:
            store.close()
        return 0

    base_url = os.environ.get("LUMEN_BASE_URL")
    token = os.environ.get("LUMEN_TOKEN")
    missing = [name for name, value in (("LUMEN_BASE_URL", base_url), ("LUMEN_TOKEN", token)) if not value]
    if missing:
        print(f"error: set {' and '.join(missing)} in the environment", file=sys.stderr)
        return 2

    store = Store(args.db)
    try:
        store.load_config(json.loads(Path(args.config).read_text()))
        connector = LumenConnector(HttpClient(base_url, token))
        run_id = run_sync(connector, store, base_url)
        print_coverage(store, run_id)
        print(f"\nDatabase: {Path(args.db).resolve()}")
        outcome = store.db.execute("SELECT outcome FROM sync_runs WHERE run_id = ?", (run_id,)).fetchone()[0]
    finally:
        store.close()
    # 0 = complete or partial (some resources collected), 1 = nothing collected.
    # A scheduler can alert on 1 without failing every run that is missing audit.
    return 1 if outcome == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
