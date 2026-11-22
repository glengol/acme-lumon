# Plan: Acme / Lumen integration

Written before building. Each step is finished before the next one starts, and later steps are refined from what earlier steps find.

## Goal

Connect to Acme's Lumen admin API. Pull users, groups, agents, connections and the audit log into a local SQLite database. Answer the assignment's questions from that database. Report the security findings to Acme.

"Today" is **2026-11-09** (given in the assignment).

## Step 1: Test and map the API
Nothing is assumed or built until this step is done.

For every endpoint in `API.md` (`/health`, `/v1/users`, `/v1/groups`, `/v1/agents`, `/v1/connections`, `/v1/audit`):
- Call it as documented and save the full response.
- Call it with no token and with a wrong token.
- Call it with invalid parameters, for example `page=0`, `page=abc`, a page past the last one, and an invalid `since`.
- Fetch every page of each paginated endpoint.
- Record the status code and response body of every call.
- Compare what came back with `API.md` and note every difference.

Output: the **API map** section below, listing each endpoint, how it behaves, and every difference from the docs.

## Step 2: Examine the data
Using the responses saved in step 1:
- List every field and value for each object type, and check them for consistency.
- Check every reference between objects: owner ids, `authorized_by`, group members, `connection_id`.
- Write down anything missing, inconsistent or surprising.

Output: the **Data map** section below.

## Step 3: Decide on definitions
From the step 2 output, settle what the questions need and record each decision with its reason:
- What a "living owner" is (question 1).
- What a "sensitive system" is, what "outside Acme" is, and how to find who authorized a path (question 2).
- Who counts as an "admin", and how to treat missing or stale data (question 3).

Output: the **Decisions** section below.

## Step 4: Design the schema
Based on steps 1–3. Requirements from the assignment:
- Running the program twice must not duplicate rows.
- The program must record what it collected and what it could not.
- A second app should be able to reuse the same shape.

**Result:** `lumen_sync/schema.sql`. Summary:
- **Run tracking:** `sync_runs` (one row per run) and `coverage` (one row per resource per run: ok or failed, HTTP status, error, record count).
- **Raw data:** `raw_records` keeps every object exactly as received.
- **Entities**, each keyed on `(source_app, external_id)`: `identities`, `groups`, `group_members`, `agents`, `agent_tools`, `connections`, `events`.
- **Policy config**, loaded from `config.json` on every `sync` and `report`: `app_classification`, `internal_domains`, `admin_groups`.

Refresh rules (after the simplification review):
1. Each snapshot resource is fetched completely in memory first (all pages). Only then are its rows replaced (delete + insert) in one transaction.
2. The audit log is append-only: fetched incrementally (`since` = newest stored event minus 24 h) and never deleted.
3. If a resource fails, nothing for it is written. Its old rows stay, and coverage records the failure.

## Step 5: Build the collector
A program that reads the base URL and token from the environment, pulls every endpoint, writes to SQLite, keeps going when an endpoint fails, and prints a coverage report.

**Result:** `python3 -m lumen_sync sync`. Layout:
- `http.py`: GET JSON with bearer auth and a 15 s timeout. Retries network errors, 429 and 5xx with backoff. Never retries other 4xx.
- `connectors/base.py`: the app-neutral contract. A connector lists `Resource`s, and each one has a name, the tables it owns, `fetch()` and `to_rows()`.
- `connectors/lumen.py`: the only Lumen-specific code. Paginates by `total_pages`, deduplicates by id, aborts a resource if `total_pages` changes mid-fetch, and never sends `X-Lumen-Scopes` (D1).
- `store.py`: replaces each resource's rows in one transaction, and loads `config.json` into the policy tables.
- `runner.py`: runs each resource in its own try/except, writes coverage either way, and prints the coverage report.

First run: users 12, groups 4 (13 memberships), agents 8 (9 tools), connections 5; audit **missing** (403, `audit.read`). A second run gives identical row counts.

## Step 6: Write and run the queries
SQL for questions 1–3, run against the database, with the results kept for `submission.md`.

**Result:** `python3 -m lumen_sync report` runs every `queries/q*.sql` file.
- `lumen_sync/views.sql` (created on every run, app-neutral):
  - `v_identities`: adds `is_active` and `is_internal`.
  - `v_admins`: recursive expansion of the admin groups, with a cycle guard and `admin_via`.
  - `v_agent_paths`: one row per tool, with app, `is_sensitive`, `is_external`, owner and authorizer.
- Results match the expected answers from step 3:
  - Q1: a-201 Finance Copilot (owner deprovisioned), a-202 Shadow Research Bot (no owner).
  - Q2: 5 paths. a-201 holds both a payroll read and an external webhook (`agent_combines = YES`).
  - Q3: u-1002 James Miller (MFA unknown, 60 days, admin via the nested IT group).

## Step 7: Verify
- Run the program twice and compare the row counts.
- Run it while part of the API is unavailable and check it still finishes with a coverage report.
- Check each answer by hand against the raw responses from step 1.

**Result:**
| Check | Outcome |
| --- | --- |
| Run twice on the same DB | Identical row counts and an identical content hash across all data tables |
| Wrong token after a good run | All resources 401, explained in coverage; existing data untouched; exit code 1 |
| API unreachable after a good run | All resources `network_error`, explained in coverage; existing data untouched; exit code 1 |
| Missing env vars | Clear error, exit code 2 |
| Answers vs raw responses | Q1–Q3 match the independent hand analysis from step 2 |
| Automated | `python3 -m unittest -v`: 23 tests. 13 run the collector against a fake API with invented data, 7 check the analysis rules on hand-built databases, and 3 run an invented second app through the same code to prove reuse (nested groups and cycles, every Q1/Q3 reason, sensitive/external rules, Q2 authorizers). The collector tests cover: paging past the end, 403, total failure, wrong token, normalization, incremental and late audit events, a growing audit log, missing fields in answers, admin group matching, redirects, Retry-After) |

Problems found and fixed during verification:
1. **The report claimed data was unusable after a failed run, even though the previous copy was intact.** It now prints per-resource freshness: when each resource was last collected, and whether its latest attempt failed (STALE / NEVER COLLECTED).
2. **The exit code was 0 even when nothing was collected.** It is now 1 when every resource fails, so a scheduler can alert. A partial run (audit missing) still exits 0.

Known limitation: when the API is unreachable, each resource retries separately (about 18 s total). That is acceptable at this size; in production, check `/health` once first.

## Step 8: Write it up
- `README.md`: how the program works and how to run it.
- `submission.md`: production next steps, AI tool use (with one correction), SQL and results, question 4, and the customer summary.

---

## API map

### Authentication
| Call | Status | Body |
| --- | --- | --- |
| No token | 401 | `{"error":"unauthorized"}` |
| Wrong token | 401 | `{"error":"unauthorized"}` |
| Token without `Bearer ` prefix | 401 | `{"error":"unauthorized"}` |
| Unknown path (`/v1/nope`) | 404 | `{"error":"not_found"}` |

Matches `API.md`.

### GET /v1/users
| Call | Status | Result |
| --- | --- | --- |
| `page=1` … `page=6` | 200 | 2 users per page, `total_pages: 6`, 12 users in total |
| No `page` parameter | 200 | Same as `page=1` |
| `page=0`, `page=-1`, `page=abc` | 400 | `{"error":"invalid_page"}` |
| `page=7`, `page=50` (past the end) | 200 | **Returns page 6's users again**, with `page` echoing the requested number |

**Differences from the docs:**
- Asking for a page past the end does not return an error or an empty list. It silently repeats the last page. A client that pages "until the list is empty" never stops, and a client that keeps whatever it receives stores duplicates. The client must stop at `total_pages`.
- Page size (2) is not documented.

### GET /v1/agents
| Call | Status | Result |
| --- | --- | --- |
| No token | 401 | `{"error":"unauthorized"}` |
| `page=1` … `page=4` | 200 | 2 agents per page, `total_pages: 4`, 8 agents in total |
| No `page` parameter | 200 | Same as `page=1` |
| `page=0`, `page=abc` | 400 | `{"error":"invalid_page"}` |
| `page=5`, `page=6`, `page=99` (past the end) | 200 | **Returns page 4's agents again** |

**Differences from the docs:** the same past-the-end behavior as `/v1/users`, so it is API-wide paging behavior, not a one-off. The client must stop at `total_pages` on every paginated endpoint.

### GET /v1/groups
| Call | Status | Result |
| --- | --- | --- |
| No token | 401 | `{"error":"unauthorized"}` |
| No parameters | 200 | 4 groups, no `page` or `total_pages` in the response |
| `page=2`, `page=abc` | 200 | The parameter is ignored; the same 4 groups come back |

Matches `API.md` (not paginated). Members are typed `user` or `group`, as documented.

### GET /v1/connections
| Call | Status | Result |
| --- | --- | --- |
| No token | 401 | `{"error":"unauthorized"}` |
| No parameters | 200 | 5 connections, no `page` or `total_pages` in the response |
| `page=2`, `page=abc` | 200 | The parameter is ignored; the same 5 connections come back |

Matches `API.md` (not paginated).

**Note for both:** with no paging metadata, the response itself is the only evidence of completeness. If either list grows past some server-side limit, the API gives no way to detect truncation.

### GET /v1/audit
| Call | Status | Body |
| --- | --- | --- |
| No token / wrong token | 401 | `{"error":"unauthorized"}` |
| Valid token, with or without `since` | **403** | `{"error":"insufficient_scope","required_scopes":["audit.read"],"hint":"pass granted scopes in the X-Lumen-Scopes header"}` |
| Valid token, `since=garbage` | **403** | Same body (the scope check runs before the parameter check) |

**Differences from the docs:**
- **403 is not documented.** `API.md` lists only 400, 401 and 404.
- **The customer's token cannot read the audit log.** It is missing the `audit.read` scope, so the customer's belief that the token has "full access" is wrong.
- **The hint says to send scopes in an `X-Lumen-Scopes` request header.** That means the client declares its own permissions. We did not send this header (see Decisions).
- We could not test `since` filtering, because every call is refused before parameters are checked.

## Data map
_(filled in during step 2)_

### Users (12 records, 12 unique ids)
| id | name | type | status | mfa_enabled | last login | days before 2026-11-09 (by date) | email domain |
| --- | --- | --- | --- | --- | --- | --- | --- |
| u-1001 | John Doe | human | ACTIVE | *missing* | 2026-11-01 | 8 | acme.com |
| u-1002 | James Miller | human | ACTIVE | *missing* | 2026-09-10 | **60** | acme.com |
| u-1003 | Maya Shah | human | **`active`** | true | 2026-11-08 | 1 | acme.com |
| u-1004 | Mary Johnson | human | DEPROVISIONED | true | 2026-04-15 | 208 | acme.com |
| u-1005 | Sam Okonkwo | human | ACTIVE | true | 2026-11-07 | 2 | **partner.example** |
| u-1006 | svc-lumen | **service** | ACTIVE | *missing* | 2026-11-09 01:00 | **after "today"** (see D9) | *no email* |
| u-1007 | Alex Carter | human | ACTIVE | true | 2026-11-08 | 1 | acme.com |
| u-1008 | Pat Morgan | human | ACTIVE | true | 2026-11-05 | 4 | acme.com |
| u-1009 | Chris Bennett | human | ACTIVE | true | 2026-11-06 | 3 | acme.com |
| u-1010 | Riley Brooks | human | DEPROVISIONED | true | 2026-01-10 | 303 | acme.com |
| u-1011 | Jordan Blake | human | ACTIVE | *missing* | 2026-11-02 | 7 | acme.com |
| u-1012 | Taylor Reed | human | ACTIVE | true | 2026-08-15 | **86** | acme.com |

Observations:
- **Status casing is inconsistent:** `ACTIVE` ×9, `active` ×1, `DEPROVISIONED` ×2. Status comparisons must ignore case. The meaning of each value is not documented.
- **`mfa_enabled` is missing for 4 users** (u-1001, u-1002, u-1006, u-1011). The field is never `false`: it is either `true` or absent. Absent means MFA status is unknown, not off.
- **Email is missing for the one service account** (as `API.md` says it may be).
- **One external user:** u-1005 on `partner.example`. All other emails are `acme.com`.
- **Two ACTIVE humans have not logged in for over 30 days:** u-1002 (60 days) and u-1012 (86 days). Whether they matter for question 3 depends on whether they are admins (see Groups).
- **Two users are DEPROVISIONED** but still returned by the API. They still appear as owners and approvers elsewhere (see Connection approvers).

### Groups (4 groups, all member references resolve)
| Group | Direct members | Effective users (after expanding nested groups) |
| --- | --- | --- |
| g-10 Everyone | 8 users | 8: u-1007, u-1008, u-1001, u-1011, u-1003, u-1009, u-1012, u-1005 |
| g-20 IT | u-1002 | 1: u-1002 |
| g-30 AI Platform Admins | u-1006, u-1009, **group g-20** | 3: u-1006, u-1009, **u-1002 (via IT)** |
| g-40 Contractors | u-1005 | 1: u-1005 |

Observations:
- **Nesting:** g-20 IT sits inside g-30 AI Platform Admins. Membership must be expanded recursively, or James Miller (u-1002) is missed as an admin. Expansion needs a cycle guard; no cycle exists today.
- **No "admin" field exists in the API.** The only signal is the group name "AI Platform Admins". Treating that group as the admin set is an inference. → Confirm with Acme which groups or roles grant admin rights in Lumen.
- **Effective admins (3):**
  - u-1006 svc-lumen: **service account**, MFA field missing, last login 2026-11-09 01:00, an hour after the "today" cut-off (see D9).
  - u-1009 Chris Bennett: human, MFA true, logged in 2 days ago.
  - u-1002 James Miller: human, admin **only through nesting**, MFA field missing, last login **60 days** ago.
- **"Everyone" is not everyone:** 12 users in total, 8 in "Everyone". The 4 not in it:
  - u-1004 Mary Johnson and u-1010 Riley Brooks: DEPROVISIONED. Expected, and consistent with the status.
  - u-1006 svc-lumen: service account. Likely expected, since it is not an employee.
  - **u-1002 James Miller: active human, the only active employee not in "Everyone". Unexplained.**

  James Miller is also the admin whose MFA is unknown, who last logged in 60 days ago, and who is an admin only through the nested IT group. Together this suggests an account outside normal provisioning that holds admin rights (an inference, not confirmed). Any policy Acme applies through "Everyone" may not cover him. → Ask Acme how this account was created, whether it is still needed, and whether "Everyone"-scoped policies cover him.
- **Only DEPROVISIONED users are in no group** (u-1004, u-1010), which is consistent with their status.
- **The external user u-1005 is in Everyone**, so anything granted to "Everyone" also reaches a contractor.

### Agents (8 agents, 8 unique ids, 9 tools)
All 8 agents have status `active` (lowercase, consistent). Tool kinds: 7 `saas_connector`, 2 `http`.

| Agent | Owner (status) | Tool → target | Connection → approver (status) |
| --- | --- | --- | --- |
| a-200 IT Helpdesk | u-1003 Maya (active) | jira, read | c-301 → u-1003 Maya (active) |
| a-203 Onboarding Guide | u-1003 Maya (active) | wiki, read | **none** |
| a-204 Release Notes | u-1007 Alex (ACTIVE) | http → `hooks.acme.com` | **none** |
| a-205 Calendar Assistant | u-1009 Chris (ACTIVE) | calendar, read | c-304 → u-1009 Chris (ACTIVE) |
| a-206 Support Macros | u-1008 Pat (ACTIVE) | slack, read | c-305 → u-1008 Pat (ACTIVE) |
| a-207 Expense FAQ | u-1007 Alex (ACTIVE) | wiki, read | **none** |
| **a-201 Finance Copilot** | **u-1004 Mary (DEPROVISIONED)** | **payroll, employees.read** | c-302 → **u-1004 Mary (DEPROVISIONED)**, last used 2026-11-08 |
| | | **http → `hooks.vendor.example`** (external) | **none** |
| **a-202 Shadow Research Bot** | **null** | **google-drive, drive.readonly** | c-303 → **u-1005 Sam (ACTIVE, partner.example)**, last used 2026-11-07 |

Cross-reference checks:
- Every non-null `owner_id` exists in users. Every non-null `connection_id` exists in connections.
- Every tool's target and scopes match its connection's app and scopes.
- Every connection is used by exactly one agent; none is unused.

Observations:
- **a-201 Finance Copilot** can read payroll (employee data) **and** call an HTTP endpoint on a non-Acme domain (`hooks.vendor.example`). One agent holding both is a path for payroll data to leave Acme. Its owner and its payroll approver are the same DEPROVISIONED user, and the payroll connection was used the day before "today". The external webhook has no connection, so no approver is on record. We cannot see whether data actually went to the webhook, because audit is blocked.
- **a-202 Shadow Research Bot** has **no owner at all** and reads Google Drive through a connection approved by the external contractor. Used two days before "today".
- **4 of 9 tools have no stored connection** (2 wiki connectors, 2 HTTP hooks; one of the hooks is on a-201). For these, Lumen records no approver, and for the wiki connectors it is unclear how they authenticate. → Ask Acme.
- **a-204 Release Notes** posts to `hooks.acme.com`, an Acme domain. It counts as internal if `acme.com` subdomains are Acme-controlled. → Confirm Acme's domain list.
- **Owner ≠ approver** only on a-202 (no owner). Everywhere else, the agent owner also approved its connection.

### Connection approvers (`connections.authorized_by` → users and groups)
Every connection names who approved it. Joining that id to `/v1/users` and `/v1/groups` shows whether the approver is still someone who should hold that power.

| Connection | App | Approver | Approver status | Approver domain | Approver groups | Last used |
| --- | --- | --- | --- | --- | --- | --- |
| c-301 | jira | u-1003 | `active` (lowercase) | acme.com | Everyone | 2026-11-06 |
| c-302 | payroll | u-1004 | **DEPROVISIONED** | acme.com | **none** | **2026-11-08** |
| c-303 | google-drive | u-1005 | ACTIVE | **partner.example** | Everyone, **Contractors** | 2026-11-07 |
| c-304 | calendar | u-1009 | ACTIVE | acme.com | Everyone, AI Platform Admins | 2026-11-06 |
| c-305 | slack | u-1008 | ACTIVE | acme.com | Everyone | 2026-11-05 |

What this lets us state, and what it does not:
- **Can state:** c-302 is still in use, and the person who approved it is deprovisioned. Nobody currently at Acme is on record as accountable for it.
- **Can state:** c-303 was approved by an external account (non-Acme email domain, member of Contractors).
- **Cannot state:** that any approver was "not allowed" to approve. Lumen exposes no approval policy (which roles or groups may authorize a connection). → Question for Acme.
- **Cannot state:** what `DEPROVISIONED` means beyond its name. `API.md` does not list status values. → Question for Acme.

## Decisions
_(filled in during step 3; decisions made earlier are recorded here as they happen)_

### D1: Do not send `X-Lumen-Scopes` (decided during step 1)
- **Context:** `/v1/audit` returns 403 `insufficient_scope` and hints that the client should declare its own scopes in an `X-Lumen-Scopes` header.
- **Decision:** we never send the header. Audit is recorded as not collected, with the reason.
- **Why:** the token's permissions are what Acme granted. Declaring extra scopes ourselves would be access the customer did not authorize.
- **Follow-ups:**
  - Ask Acme to grant `audit.read` to the integration token through their normal process.
  - Report it as a finding: Lumen's API tells callers to self-declare permissions. If the server trusts that header, any token holder can read the audit log. Acme and the Lumen vendor should confirm and fix this. We did not test it.

### D2: Status is compared without case, and both forms are kept
- **Seen:** `ACTIVE`, `active`, `DEPROVISIONED`.
- **Decision:** store `status_raw` exactly as received, plus `status` lowercased. An account is active only if `status = 'active'`. Any other or unknown value counts as **not active**, so an unexpected value surfaces as a risk instead of being hidden.

### D3: Living owner (question 1)
- **Decision:** an agent has a living owner only if `owner_id` is not null, resolves to a known user, that user's status is active (D2), and the user is human.
- **Why human:** an owner is the person accountable for the agent. A service account cannot be held accountable. (No agent is owned by a service account today; the rule is for future data.)
- **Each failing agent gets a reason:** `no owner` / `owner not found` / `owner not active (<status>)` / `owner is a service account`.

### D4: Sensitive system (question 2)
- **Seen targets:** jira, wiki, calendar, slack, payroll, google-drive, plus 2 HTTP URLs.
- **Rule: if in doubt, it is sensitive.** Sensitivity is a **config list**, not logic in the code. An app counts as non-sensitive only if it is explicitly listed as such; **any app not on the list is sensitive by default**, including apps that appear in the future.
  - **Sensitive:** `payroll` (employee personal and pay data), `google-drive` (a broad file store), `slack` (free-text messages that often hold credentials, customer and HR content), `calendar` (meeting titles, attendees and notes).
  - **Not sensitive (explicitly listed):** `jira` (read, helpdesk tickets), `wiki` (read, internal docs).
  - **Any write or admin scope** on any app counts as sensitive. None is seen today.
- → Ask Acme to confirm the whole classification, including jira and wiki.

### D5: Outside Acme (question 2)
- **Seen:** HTTP hosts `hooks.acme.com` and `hooks.vendor.example`. Email domains `acme.com` and `partner.example`.
- **Decision:** a **config list of Acme domains**, starting with `["acme.com"]`. A host or email is internal if it equals an Acme domain or is a subdomain of one; everything else is external.
  - An `http` tool with an external host = **sends data outside Acme**.
  - A user with an external email domain = **external identity**.
- → Confirm Acme's full domain list.

### D6: Who authorized the path (question 2)
- **Decision:** for a tool backed by a connection, the authorizer is `connection.authorized_by`, shown with that user's status and domain. For a tool with no connection, the authorizer is **"none on record"**.
- **Why not fall back to the agent owner:** the API does not say the owner approved it. Filling the gap with a guess would hide the real finding, which is that nobody is on record as approving this path.
- The agent owner is shown next to the authorizer, so both are visible.

### D7: Admin (question 3)
- **Seen:** no admin field in the API. One group is named "AI Platform Admins", and it contains a nested group.
- **Decision:** admins are the **effective members** (expanded recursively, with a cycle guard) of a **config list of admin groups**, starting with `["AI Platform Admins"]`. Each config entry matches a group **by id, or by name ignoring case and surrounding spaces**. An entry that matches no group is reported as a WARNING, never silently treated as "no admins". `admin_via` shows the shortest path.
- Question 3 asks for **human** admins only. Service-account admins are listed separately as their own finding. An admin whose account is not active, whose type is unknown, or whose id is not a known user is **kept** in Q3 with that as the reason (D2: unknown values surface as risk).
- → Confirm with Acme which groups or roles grant admin rights.

### D8: Missing MFA (question 3)
- **Seen:** `mfa_enabled` is either `true` or absent, never `false`.
- **Decision:** an admin is flagged if `mfa_enabled` is not `true`. The output distinguishes **`off`** (false) from **`unknown`** (absent), and the summary says "MFA not confirmed", never "MFA off", for absent values.

### D9: Not logged in for 30 days (question 3)
- **Decision:** "today" is **2026-11-09** (from the assignment). Days are counted by calendar date. Stale means the last login date is before **2026-10-10**, is null, or is not a readable ISO-8601 timestamp (if in doubt, flag it).
- **Caveat:** the only login data we have is `last_login_at` on the user record. The audit log, which holds login events, is blocked (D1), so we cannot cross-check it.
- **Edge case:** svc-lumen's last login (2026-11-09 01:00) is *after* the 00:00 cut-off. It does not change any answer (it is recent, and Q3 is humans only), but it shows the data is newer than the assignment's "today". We treat "today" as the reporting date, not as the time the data was pulled.
