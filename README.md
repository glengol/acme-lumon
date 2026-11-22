# Acme × Lumen: agent and access inventory

A small program that connects to Acme's **Lumen** admin API, copies users, groups, agents and connections into a local **SQLite** database, and answers security questions from that database with plain SQL.

It is safe to run again and again (no duplicate rows), and it still finishes when part of the API refuses or fails, telling you what it could not collect and why.

---

## 1. Prerequisites

| What | Version | Check with | Notes |
| --- | --- | --- | --- |
| Python | 3.10 or newer | `python3 --version` | Standard library only. Nothing to `pip install`. |
| SQLite | 3.25 or newer | bundled with Python | Optional: the `sqlite3` CLI if you want to query the database by hand. |
| The Lumen mock API | from the assignment | `curl -s http://127.0.0.1:8787/health` | Not included in this repo. Start it from the assignment folder (step 2). |

---

## 2. Run it, step by step

**Step 1: get the code**
```bash
git clone https://github.com/glengol/acme-lumon.git
cd acme-lumon
```

**Step 2: start the Lumen mock** (in a separate terminal, from the assignment folder)
```bash
python3 mock_server.py
```
Check that it is up:
```bash
curl -s http://127.0.0.1:8787/health      # {"status": "ok"}
```

**Step 3: point the program at the API**
```bash
export LUMEN_BASE_URL=http://127.0.0.1:8787
export LUMEN_TOKEN=lumen-test-token
```

**Step 4: collect the data**
```bash
python3 -m lumen_sync sync
```
This creates `lumen.db` in the current folder and prints a coverage report:
```text
Coverage report: run 1, lumen at http://127.0.0.1:8787, outcome: PARTIAL
------------------------------------------------------------------------------
  [ok]      users          12 records, 6 pages
  [ok]      groups          4 records
  [ok]      agents          8 records, 4 pages
  [ok]      connections     5 records
  [MISSING] audit        HTTP 403 insufficient_scope
                         token lacks the permission for this endpoint; required scopes: audit.read; ...
------------------------------------------------------------------------------
  Not collected this run: audit.
```
`PARTIAL` is expected: Acme's token does not have the `audit.read` permission (see [section 5](#5-how-the-api-pushed-back-and-what-we-did)).

**Step 5: answer the questions**
```bash
python3 -m lumen_sync report
```
This shows how fresh each part of the data is, then runs every query in `queries/` and prints the results as tables.

**Step 6 (optional): run the tests**
```bash
python3 -m unittest -v
```
These are 6 tests against a small fake API, so the mock does not need to be running.

**Step 7 (optional): explore the database yourself**
```bash
sqlite3 lumen.db
sqlite> .read queries/q1_agents_without_living_owner.sql
```

### Options

| Setting | How | Default |
| --- | --- | --- |
| API address | `LUMEN_BASE_URL` (required) | – |
| API token | `LUMEN_TOKEN` (required) | – |
| Database file | `--db path` or `LUMEN_DB` | `lumen.db` |
| Policy config | `--config path` | `lumen_sync/config.json` |

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | Complete or partial: at least some data was collected |
| `1` | Nothing was collected (for example a wrong token or the API is down). Existing data is kept. |
| `2` | Configuration error (for example a missing environment variable) |

---

## 3. How it works

The program has three stages. Each stage only talks to the next one.

```
 Lumen API  ──(1) COPY──►  raw JSON  ──(2) MAP──►  SQLite tables + views  ──(3) ASK──►  answers
            sync                       store                                  report
```

| Stage | What happens | Where |
| --- | --- | --- |
| **1. Copy** | Calls each endpoint, reads every page, and removes duplicates. A failing endpoint is recorded, and the next one still runs. | `http.py`, `connectors/lumen.py` |
| **2. Map** | Turns Lumen's objects into app-neutral tables and cleans up inconsistent values. Each endpoint's data is replaced in one transaction. | `connectors/lumen.py`, `store.py` |
| **3. Ask** | Shared logic lives in SQL views (who is active, who is an admin, what is sensitive or external). Each question is a short SQL file on top of the views. | `views.sql`, `queries/` |

### Project layout

| Path | Purpose |
| --- | --- |
| `lumen_sync/__main__.py` | Command line: `sync` and `report` |
| `lumen_sync/http.py` | HTTP client: token, timeout, retries |
| `lumen_sync/connectors/base.py` | The contract every source app follows |
| `lumen_sync/connectors/lumen.py` | Everything Lumen-specific: endpoints, paging, field mapping |
| `lumen_sync/runner.py` | Runs each endpoint, records coverage, prints the coverage report |
| `lumen_sync/store.py` | Writes to SQLite safely and loads the policy config |
| `lumen_sync/schema.sql` | The tables |
| `lumen_sync/views.sql` | The shared analysis logic |
| `lumen_sync/config.json` | Acme's policy: sensitive apps, Acme domains, admin groups |
| `queries/q1…q3.sql` | One file per question |
| `tests/test_sync.py` | Automated tests |
| `PLAN.md` | The plan, the API and data findings, and every decision with its reason |
| `submission.md` | Production notes, AI tool use, answers, and the customer summary |

---

## 4. The database

### Tables

| Table | Holds | From Lumen |
| --- | --- | --- |
| `identities` | People and service accounts | `/v1/users` |
| `groups`, `group_members` | Groups. A member is a user or another group. | `/v1/groups` |
| `agents`, `agent_tools` | AI agents and each tool they can use | `/v1/agents` |
| `connections` | Stored app logins, and who approved them | `/v1/connections` |
| `events` | Activity log (empty until access is granted) | `/v1/audit` |
| `raw_records` | Every object exactly as received | all |
| `sync_runs`, `coverage` | Each run, and per endpoint: collected or not, and why | – |
| `app_classification`, `internal_domains`, `admin_groups` | Acme's policy, loaded from `config.json` | – |

### Views

| View | Adds |
| --- | --- |
| `v_identities` | `is_active` (status, ignoring case) and `is_internal` (email on an Acme domain) |
| `v_admins` | Everyone in the admin groups, **including through nested groups**, and how (`admin_via`) |
| `v_agent_paths` | One row per agent tool: what it reaches, `is_sensitive`, `is_external`, owner and approver |

### Cleaning rules

| Raw value | Stored as | Why |
| --- | --- | --- |
| `status`: `ACTIVE`, `active` | `status_raw` as received, plus `status` lowercased | The same state arrives in different casing |
| `mfa_enabled` missing | `NULL` (unknown), not `false` | Missing data is not the same as MFA being off |
| Email | Also stored as `email_domain` | To tell Acme staff from outside people |
| HTTP tool URL | Also stored as `target_host` | To tell Acme hosts from outside hosts |

### Safe to refresh

- Each endpoint is fetched **completely** first. Only then are its rows replaced, **in one transaction**.
- Running twice gives identical data, so there are no duplicates.
- If an endpoint fails, its previous data is **kept**, and the report marks it **STALE**.

---

## 5. How the API pushed back, and what we did

| What we found | What the program does |
| --- | --- |
| Asking for a page past the last one **repeats the last page** (undocumented) | Stops at `total_pages`, removes duplicates by id, and stops if `total_pages` changes partway through |
| `/v1/audit` returns **403 `insufficient_scope`** (undocumented): the token lacks `audit.read` | Records it in coverage and carries on with the other endpoints |
| The 403 suggests **sending your own scopes in an `X-Lumen-Scopes` header** | **We never send it.** Permissions come from the token Acme issued, not from us. Reported as a finding instead. |
| 401 / 403 / 404 | Not retried: they will not succeed on a second try |
| Network errors, 429, 5xx | Retried 3 times with backoff |

The full probe results are in `PLAN.md` under *API map*.

---

## 6. Changing the policy

The analysis rules are data in `lumen_sync/config.json`, not code:

```json
{
  "internal_domains": ["acme.com"],
  "admin_groups": { "lumen": ["AI Platform Admins"] },
  "app_classification": {
    "payroll": { "sensitive": true,  "reason": "employee personal and pay data" },
    "jira":    { "sensitive": false, "reason": "read-only helpdesk tickets" }
  }
}
```

- An app that is **not listed is treated as sensitive** ("if in doubt, sensitive").
- After editing, run `sync` again to reload the config, then run `report`.

---

## 7. Adding a second app

Only the first stage is Lumen-specific. To add another platform:

1. Add `lumen_sync/connectors/<app>.py` with a class that lists its resources (name, tables, fetch, map to rows), like `LumenConnector`.
2. Register it in `__main__.py`.

The tables, the views, the queries, the coverage report and the refresh logic stay the same. Every row carries a `source_app` column, so several apps can share one database.
