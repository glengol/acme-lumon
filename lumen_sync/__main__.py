"""Command line entry point.

    python3 -m lumen_sync sync      pull from the source app into SQLite, print coverage
    python3 -m lumen_sync report    answer the questions from the database

The source app is chosen with --app (default: lumen). Its address and token
come from <APP>_BASE_URL and <APP>_TOKEN, e.g. LUMEN_BASE_URL / LUMEN_TOKEN.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from .connectors import CONNECTORS
from .http import HttpClient
from .report import print_report
from .runner import print_coverage, run_sync
from .store import Store

DEFAULT_CONFIG = Path(__file__).with_name("config.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lumen_sync")
    parser.add_argument("command", choices=["sync", "report"])
    parser.add_argument("--app", default="lumen", choices=sorted(CONNECTORS), help="source app (default: lumen)")
    parser.add_argument("--db", help="SQLite file (default: <APP>_DB, else <app>.db, e.g. lumen.db)")
    parser.add_argument("--allow-empty", action="store_true",
                        help="accept an empty list for a resource that has stored rows (normally refused)")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="policy config JSON")
    args = parser.parse_args(argv)
    args.db = args.db or os.environ.get(f"{args.app.upper()}_DB") or f"{args.app}.db"

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

    prefix = args.app.upper()
    url_var, token_var = f"{prefix}_BASE_URL", f"{prefix}_TOKEN"
    base_url = os.environ.get(url_var)
    token = os.environ.get(token_var)
    missing = [name for name, value in ((url_var, base_url), (token_var, token)) if not value]
    if missing:
        print(f"error: set {' and '.join(missing)} in the environment", file=sys.stderr)
        return 2

    host = (urlparse(base_url).hostname or "").lower()
    if urlparse(base_url).scheme != "https" and host not in ("localhost", "127.0.0.1", "::1"):
        print(f"warning: {base_url} is not https; the token will be sent unencrypted", file=sys.stderr)

    store = Store(args.db)
    try:
        store.load_config(json.loads(Path(args.config).read_text()))
        connector = CONNECTORS[args.app](HttpClient(base_url, token))
        run_id = run_sync(connector, store, base_url, allow_empty=args.allow_empty)
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
