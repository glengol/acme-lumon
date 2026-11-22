# Submission

Covers parts 2 to 5 of the assignment. Part 1 (the solution, and how to run it) is in [`README.md`](README.md). The plan written before building, with every API finding and decision, is in [`PLAN.md`](PLAN.md).

All results below come from a fresh run against the Lumen API, with **9 November 2026** as "today".

---

## 2. Production

What I would change before running this for a real customer, most important first.

| Area | Today | In production |
| --- | --- | --- |
| **Token** | Read from an environment variable | Stored in a secrets manager and rotated. Ask the vendor for a read-only, least-privilege integration token that includes `audit.read`. |
| **Scheduling and alerting** | Run by hand | Run on a schedule (for example hourly). Alert on exit code `1` (nothing collected), and on any resource that stays STALE longer than a set time. |
| **Audit log** | Ready but never run against Lumen (access denied). Incremental (`since` = newest stored event minus 24 hours, explicit epoch on the first run), append-only, follows paging and tolerates a growing log; tested against a fake API | Run it against Lumen as soon as `audit.read` is granted, and confirm Lumen's retention window, paging and event detail before relying on it |
| **Change history** | Each run replaces the previous copy | Keep snapshots, or a change log per entity, so "new agent", "owner changed" and "scope added" become alerts instead of silent updates. |
| **Paging** | Stops at `total_pages`; aborts if it changes mid-fetch | Ask the vendor for cursor-based paging or a snapshot token. Add a page-size check to detect quiet truncation on the unpaginated endpoints. |
| **Resilience** | Retries 5xx and network errors (honouring `Retry-After`); never retries other 4xx; stops after a 401; refuses redirects | Check `/health` once before starting, and add a total time budget per run. |
| **Reuse by a second app** | Connector registry, neutral vocabulary, per-resource replace and cross-app identity linking; proven by a test with an invented second app | A real second connector against a real API; per-tenant policy tables; show `source_app` in the answers once there is more than one app |
| **Schema changes** | `CREATE TABLE IF NOT EXISTS`; a changed schema is not applied to an existing database | Versioned migrations. |
| **Reporting date** | "Today" (2026-11-09) is written into the Q3 SQL, as the assignment fixes it | A parameter, defaulting to the time of the last successful sync. |
| **API drift** | Unknown fields are kept in `raw_records` | Contract tests against the vendor API, plus alerts when new fields, new status values or new tool kinds appear. |
| **Storage** | Local SQLite file | A managed database per tenant, encrypted at rest. The data contains personal information (names, emails, login times), so it needs access control and a retention policy. |
| **Policy config** | `config.json` in the repo | Owned and approved by the customer, versioned, with a record of who changed what. |
| **Findings** | SQL queries printed to a terminal | Findings become tracked records with an owner, a status and a due date, and feed into Reco's product rather than a report. |
| **Observability** | Coverage report on stdout | Structured logs and metrics per run and per resource: records, pages, latency, errors. |
| **Vendor issue** | Reported in this summary | Responsible disclosure to Lumen of the `X-Lumen-Scopes` hint (see the customer summary, finding 3). |

---

## 3. AI tools

**Tool:** Claude Code (Anthropic), used in the terminal as a pair programmer for the whole assignment.

**How I used it:**
- **Reading and planning.** It read the brief and the API docs and drafted the plan. I made it work one step at a time and explain each step before moving on.
- **Probing the API.** It ran the `curl` calls and the scripts that mapped every endpoint and every inconsistency (`PLAN.md`, API map and Data map).
- **Writing code.** The collector, the schema, the views and the SQL queries, built to the decisions recorded in `PLAN.md`.
- **Verifying.** Running twice, running with a wrong token or a dead API, and writing the automated tests.

**Where I rejected or corrected its output:**

1. **The plan was full of assumptions.** The first draft of `PLAN.md` already contained "findings" and definitions (what counts as sensitive, how to treat missing MFA, how refusals behave) before a single API call had been made. I rejected it and had it rewritten as a plan of actions only: test and map everything first, then decide definitions from what the data actually shows. All decisions (D1–D9 in `PLAN.md`) were made only after the data was examined.

2. **The schema was over-engineered.** I asked whether it was. It had run-history columns on every table, five config tables and redundant columns. We cut it to what the assignment needs: a per-resource replace in one transaction, three config tables, and no "current row" filtering in queries. It is just as safe and simpler to explain.

3. **Facts versus inference.** When it reported that a connection's approver was "deprovisioned", I made it show the raw API record. That led to separating what the data proves (the `status` value) from what it implies (the meaning of `DEPROVISIONED` is not documented). Unproven points became questions for Acme instead of statements. The same rule applies to "external approver" versus "approver from the wrong group".

4. **A policy choice I made differently.** It classified Slack and calendar as non-sensitive by default. I chose "if in doubt, it is sensitive": any app not explicitly cleared counts as sensitive, and Acme confirms the list.

Verification also caught two bugs in its code, both now fixed: the report claimed data was unusable after a failed run (it was intact), and the exit code was `0` even when nothing was collected.

**Outside reviews.** After the first version, the solution was reviewed three times from a fresh clone:
- **Two rounds by a friend**, running their own AI assistant. Their main catches: SQL string concatenation with a NULL field blanked out the `authorized_by` cell in Q2; a trailing space in a group name silently produced zero admins; and the audit design would have missed events logged late.
- **One independent AI review** that I ran myself, with no access to my notes. It found that Q3 hid admins whose account was not active, and that malformed dates and missing references could fall out of the answers.

I fixed what held up, one change per commit. Most fixes are covered by a test, either in the same commit or a later one (the analysis-rule tests were added together at the end). A few small ones were checked by hand only: the agent status column, the https warning, enforcing foreign keys, and the error for a missing database file.

---

## 4. Answers

The views these queries use (`v_identities`, `v_admins`, `v_agent_paths`) are in [`lumen_sync/views.sql`](lumen_sync/views.sql). Policy values (sensitive apps, Acme domains, admin groups) come from [`lumen_sync/config.json`](lumen_sync/config.json). Run all three with `python3 -m lumen_sync report`.

### Q1. Which agents have no living owner?

A living owner is a user who exists, is active (ignoring case) and is human.

```sql
SELECT a.external_id                                   AS agent_id,
       a.name                                          AS agent,
       COALESCE(a.status_raw, '-')                     AS agent_status,
       COALESCE(a.owner_id, '-')                       AS owner_id,
       COALESCE(o.display_name, '-')                   AS owner,
       CASE
           WHEN a.owner_id IS NULL        THEN 'no owner set'
           WHEN o.external_id IS NULL     THEN 'owner id not found in users'
           WHEN o.is_active = 0           THEN 'owner not active (' || COALESCE(o.status_raw, 'no status') || ')'
           WHEN o.kind IS NULL            THEN 'owner kind unknown'
           WHEN o.kind <> 'human'         THEN 'owner is not a human (' || o.kind || ')'
       END                                             AS reason,
       (SELECT group_concat(COALESCE(t.target_host, t.target, '-'), ', ')
          FROM agent_tools t
         WHERE t.source_app = a.source_app AND t.agent_id = a.external_id)  AS reaches,
       (SELECT MAX(c.last_used_at)
          FROM agent_tools t JOIN connections c
            ON c.source_app = t.source_app AND c.external_id = t.connection_id
         WHERE t.source_app = a.source_app AND t.agent_id = a.external_id)  AS last_used_at
FROM agents a
LEFT JOIN v_identities o ON o.source_app = a.source_app AND o.external_id = a.owner_id
WHERE a.owner_id IS NULL
   OR o.external_id IS NULL
   OR o.is_active = 0
   OR COALESCE(o.kind, '') <> 'human'
ORDER BY (a.status = 'active') DESC, a.external_id;
```

| agent_id | agent | agent_status | owner_id | owner | reason | reaches | last_used_at |
|---|---|---|---|---|---|---|---|
| a-201 | Finance Copilot | active | u-1004 | Mary Johnson | owner not active (DEPROVISIONED) | payroll, hooks.vendor.example | 2026-11-08T14:05:00Z |
| a-202 | Shadow Research Bot | active | - | - | no owner set | google-drive | 2026-11-07T09:12:00Z |

### Q2. Which agents can reach a sensitive system or send data outside Acme, and who authorized that path?

- **Sensitive:** any app not explicitly cleared in `config.json` ("if in doubt, sensitive"), or any write or admin scope.
- **External:** a tool that calls a URL (Lumen's `http` kind, stored as the neutral `outbound_url`) whose host is not `acme.com` or a subdomain of it.
- **Authorizer:** the connection's `authorized_by`. If the tool has no stored connection, it is "none on record"; it is never filled in with the owner.
- **`agent_combines = YES`** marks an agent that holds both a sensitive read and an external send.

```sql
SELECT p.agent_id,
       p.agent_name                                                AS agent,
       COALESCE(p.agent_status, '-')                               AS agent_status,
       -- Every concatenated part is wrapped in COALESCE: in SQL, 'x' || NULL is NULL,
       -- and one missing field must never blank out the whole cell.
       CASE WHEN p.owner_id IS NULL  THEN 'no owner set'
            WHEN p.owner_ref IS NULL THEN p.owner_id || ' (not found in users)'
            ELSE COALESCE(p.owner_name, p.owner_id) || ' (' || COALESCE(p.owner_status, 'no status') || ')'
       END                                                         AS owner,
       COALESCE(CASE WHEN p.tool_kind = 'outbound_url' THEN p.target ELSE p.app END, '-') AS reaches,
       CASE WHEN p.is_sensitive = 1 THEN 'sensitive: ' || COALESCE(p.app, 'unknown app')
            ELSE 'external: ' || COALESCE(p.target_host, 'unparseable URL ' || COALESCE(p.target, '-'))
       END                                                         AS why,
       COALESCE(p.tool_scopes, '[]')                               AS scopes,
       CASE WHEN p.connection_id IS NULL  THEN 'none on record (no stored connection)'
            WHEN p.connection_ref IS NULL THEN 'unknown (connection ' || p.connection_id || ' not found)'
            WHEN p.authorizer_id IS NULL  THEN 'none on record (connection has no authorizer)'
            WHEN p.authorizer_ref IS NULL THEN p.authorizer_id || ' (not found in users)'
            ELSE COALESCE(p.authorizer_name, p.authorizer_id) || ' (' || p.authorizer_id || ', '
                 || COALESCE(p.authorizer_status, 'no status')
                 || CASE WHEN p.authorizer_is_internal = 0
                         THEN ', external: ' || COALESCE(p.authorizer_domain, '-') ELSE '' END
                 || ')'
       END                                                         AS authorized_by,
       COALESCE(p.connection_last_used_at, '-')                    AS last_used_at,
       CASE WHEN EXISTS (SELECT 1 FROM v_agent_paths s
                          WHERE s.source_app = p.source_app AND s.agent_id = p.agent_id AND s.is_sensitive = 1)
             AND EXISTS (SELECT 1 FROM v_agent_paths x
                          WHERE x.source_app = p.source_app AND x.agent_id = p.agent_id AND x.is_external = 1)
            THEN 'YES' ELSE '' END                                 AS agent_combines
FROM v_agent_paths p
WHERE p.is_sensitive = 1 OR p.is_external = 1
ORDER BY (p.agent_status = 'active') DESC, agent_combines DESC, p.agent_id, p.tool_id;
```

| agent_id | agent | agent_status | owner | reaches | why | scopes | authorized_by | last_used_at | agent_combines |
|---|---|---|---|---|---|---|---|---|---|
| a-201 | Finance Copilot | active | Mary Johnson (deprovisioned) | payroll | sensitive: payroll | ["employees.read"] | Mary Johnson (u-1004, deprovisioned) | 2026-11-08T14:05:00Z | YES |
| a-201 | Finance Copilot | active | Mary Johnson (deprovisioned) | https://hooks.vendor.example/payroll | external: hooks.vendor.example | [] | none on record (no stored connection) | - | YES |
| a-202 | Shadow Research Bot | active | no owner set | google-drive | sensitive: google-drive | ["drive.readonly"] | Sam Okonkwo (u-1005, active, external: partner.example) | 2026-11-07T09:12:00Z |  |
| a-205 | Calendar Assistant | active | Chris Bennett (active) | calendar | sensitive: calendar | ["read"] | Chris Bennett (u-1009, active) | 2026-11-06T08:45:00Z |  |
| a-206 | Support Macros | active | Pat Morgan (active) | slack | sensitive: slack | ["read"] | Pat Morgan (u-1008, active) | 2026-11-05T14:10:00Z |  |

### Q3. Which human admins are missing MFA or have not logged in for 30 days?

- **Admins:** everyone in "AI Platform Admins", including through nested groups.
- **MFA:** flagged unless confirmed on. A missing value is shown as "unknown", not "off".
- **Stale:** no login since 2026-10-10 (30 days before 9 November 2026), none recorded, or a login value that is not a readable timestamp. Days are counted by calendar date.
- **Also listed:** admins whose account is not active, whose type is unknown, or who are not found in users. Holding admin rights in that state is itself a risk.

```sql
WITH admins AS (
    SELECT a.*,
           CASE WHEN a.last_login_at GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T*'
                 AND julianday(substr(a.last_login_at, 1, 10)) IS NOT NULL
                THEN substr(a.last_login_at, 1, 10) END       AS login_date   -- NULL = missing or unreadable
    FROM v_admins a
    WHERE COALESCE(a.kind, '') <> 'service'
)
SELECT a.admin_id                                             AS user_id,
       COALESCE(a.display_name, '-')                          AS admin,
       COALESCE(a.status_raw, '-')                            AS status,
       a.admin_via                                            AS admin_via,
       CASE a.mfa_enabled WHEN 1 THEN 'on' WHEN 0 THEN 'off' ELSE 'unknown (not reported)' END AS mfa,
       COALESCE(a.login_date, COALESCE(a.last_login_at, 'never')) AS last_login,
       CAST(julianday('2026-11-09') - julianday(a.login_date) AS INTEGER) AS days_since_login,
       trim(
         CASE WHEN a.external_id IS NULL          THEN 'admin member not found in users; ' ELSE '' END ||
         CASE WHEN a.external_id IS NOT NULL AND a.is_active = 0
                                                  THEN 'account not active (' || COALESCE(a.status_raw, 'no status') || '); ' ELSE '' END ||
         CASE WHEN a.external_id IS NOT NULL AND a.kind IS NULL
                                                  THEN 'account type unknown; ' ELSE '' END ||
         CASE WHEN a.mfa_enabled IS NOT 1          THEN 'MFA not confirmed; ' ELSE '' END ||
         CASE WHEN a.last_login_at IS NULL         THEN 'no login recorded'
              WHEN a.login_date IS NULL            THEN 'login date unreadable'
              WHEN a.login_date < '2026-10-10'     THEN 'no login in 30 days'
              ELSE '' END,
         '; ')                                                AS reasons
FROM admins a
WHERE a.external_id IS NULL
   OR a.is_active = 0
   OR a.kind IS NULL
   OR a.mfa_enabled IS NOT 1
   OR a.login_date IS NULL
   OR a.login_date < '2026-10-10'
ORDER BY a.admin_id;
```

| user_id | admin | status | admin_via | mfa | last_login | days_since_login | reasons |
|---|---|---|---|---|---|---|---|
| u-1002 | James Miller | ACTIVE | AI Platform Admins > IT | unknown (not reported) | 2026-09-10 | 60 | MFA not confirmed; no login in 30 days |

Not in Q3, because it is not human: the service account **svc-lumen (u-1006)** is also a direct admin, with MFA not reported. See the customer summary, finding 5.

### Q4. What, exactly, should we ask the customer to change before calling this integration done?

**For the integration itself:**
1. **Grant `audit.read` to the integration token.** Without it we cannot see what agents actually did, which is one of the four things Acme asked for. Today we report what agents *can* do, not what they *did*.
2. **Confirm the token is read-only,** and tell us every scope it holds, so "full access" is documented rather than assumed.
3. **Confirm four definitions** so the findings are based on Acme's policy, not ours:
   - which groups or roles grant admin rights in Lumen (we used "AI Platform Admins", including nested groups);
   - the full list of Acme-owned domains (we used `acme.com` and its subdomains);
   - which apps are sensitive (we treated everything except read-only jira and wiki as sensitive);
   - what each user `status` value means (we treated only "active" as active).
4. **Ask the Lumen vendor three things:**
   - whether the API trusts the `X-Lumen-Scopes` header, and if so, to fix it;
   - to report MFA for every user (it is missing for 4 accounts);
   - to record an approver for tools that have no stored connection.

**Security changes we recommend before relying on the results** (detail in the summary below):
- Disable or re-own Finance Copilot, and remove its external payroll webhook.
- Assign an owner to Shadow Research Bot or disable it, and review the contractor-approved Drive access.
- Confirm MFA for James Miller and svc-lumen, and review whether James still needs admin rights.

---

## 5. Customer summary

> **To:** Acme Security
> **Subject:** Lumen AI agents: first security review (data as of 9 November 2026)
>
> This summary can be forwarded to IT as is. Findings are ordered by importance. Each one says what we saw, why it matters and what to do.

**TL;DR**
1. **Act today:** "Finance Copilot" reads payroll and can send data to an outside address. Its owner and the person who approved its payroll access have both been deprovisioned, and it was used on 8 November. Disable it or remove the outside webhook.
2. **We cannot see what agents did.** The integration token is missing `audit.read`. Please grant it so we can check whether any data left Acme.
3. **Review next:** an ownerless agent reading Google Drive through a contractor's approval, and an admin (James Miller) with unconfirmed MFA and no login for 60 days.

### Finding 1 (critical): an agent whose owner has left can read payroll data and send data to an outside address

**Finance Copilot** (`a-201`) has two tools:
- it reads **payroll** (`employees.read`) through the connection `payroll-export` (`c-302`);
- it can call **`https://hooks.vendor.example/payroll`**, a host outside Acme.

The same agent can read employee pay data and send data out of the company. In addition:
- its owner, **Mary Johnson (`u-1004`), is deprovisioned**, so nobody at Acme is accountable for it;
- the payroll access was **approved by that same deprovisioned account**;
- the outside webhook has **no approval on record at all**;
- it is **still running**: the payroll connection was last used on **8 November 2026 at 14:05 UTC**.

We cannot yet see whether payroll data was actually sent to the outside address, because the audit log is not available to us (finding 3).

**Action, today:**
1. Disable Finance Copilot, or at least remove the `hooks.vendor.example` tool.
2. Revoke or re-authorize the `payroll-export` connection under a current owner.
3. Identify who operates `hooks.vendor.example`, and whether a contract covers sending payroll data there.
4. Once the audit log is available, review every Finance Copilot tool call.

### Finding 2 (high): an agent with no owner reads Google Drive through a contractor's approval

**Shadow Research Bot** (`a-202`):
- has **no owner** at all;
- reads **Google Drive** (`drive.readonly`) through the connection `gdrive-research` (`c-303`);
- that connection was approved by **Sam Okonkwo (`u-1005`)**, an external account (`partner.example`) in the Contractors group;
- it was last used on **7 November 2026**.

**Action:**
1. Assign an Acme owner, or disable the agent.
2. Confirm whether contractors may connect company data to AI agents. If not, revoke `c-303` and reconnect it under an employee.

### Finding 3 (high): we cannot see what agents did

The token Acme issued for this integration **does not have `audit.read`**. The audit endpoint returned `403 insufficient_scope`. So we cannot see logins, tool invocations or OAuth grants, which means we cannot confirm or rule out data leaving through finding 1.

**Action:** grant `audit.read` to the integration token.

> **For Acme's security team only (please do not forward more widely until the vendor responds).**
> The 403 message tells callers to declare their own permissions in an `X-Lumen-Scopes` request header. If Lumen trusts a permission the caller declares, any Lumen token holder might be able to read the audit log. **This is unconfirmed:** we deliberately did not send the header, because that would mean accessing data Acme did not grant us. Please raise it with Lumen as a possible vulnerability and ask them to confirm whether the header is trusted.

### Finding 4 (high): an admin with unconfirmed MFA who has not logged in for 60 days

**James Miller (`u-1002`)**:
- is an admin, **only through the IT group**, which is nested inside "AI Platform Admins" (easy to miss in a manual review);
- **MFA is not reported** for this account;
- **last logged in on 10 September 2026** (60 days before 9 November);
- is the **only active employee who is not in the "Everyone" group**. This suggests the account was created outside the normal process. We have not confirmed this.

**Action:**
1. Confirm whether James still needs admin rights. If not, remove IT from "AI Platform Admins", or remove James from IT.
2. Enforce MFA on the account.
3. Confirm how the account was created, and whether policies applied through "Everyone" cover it.

### Finding 5 (medium): a service account holds admin rights

**svc-lumen (`u-1006`)** is a direct member of "AI Platform Admins":
- **MFA is not reported**;
- it was active on **9 November 2026 at 01:00 UTC**.

Service accounts usually cannot use MFA, so its credentials are only as safe as where they are stored.

**Action:** confirm what svc-lumen is used for, whether it needs admin rights, where its credentials live, and when they were last rotated.

### Finding 6 (medium): four agent tools have no approval on record

These tools are not backed by a stored connection, so Lumen records nobody as approving them:

| Agent | Tool | Owner |
| --- | --- | --- |
| Finance Copilot | `https://hooks.vendor.example/payroll` (outside Acme) | Mary Johnson (deprovisioned) |
| Release Notes | `https://hooks.acme.com/releases` (Acme) | Alex Carter |
| Onboarding Guide | wiki | Maya Shah |
| Expense FAQ | wiki | Alex Carter |

**Action:** confirm who approved each one. For the wiki tools, also confirm how they authenticate.

### Finding 7 (low): two more agents read sensitive apps, with healthy ownership

**Calendar Assistant** (calendar) and **Support Macros** (Slack) are read-only, and each is owned and approved by an active employee (Chris Bennett and Pat Morgan). They are listed only because calendar and Slack content is often sensitive. **Action:** confirm that this access is intended.

### Finding 8 (low): account hygiene

| Item | Action |
| --- | --- |
| MFA not reported for John Doe (`u-1001`) and Jordan Blake (`u-1011`) | Confirm MFA |
| Taylor Reed (`u-1012`): active, no login for 86 days | Confirm the account is still needed |
| "Everyone" has 8 of 12 accounts. The two deprovisioned users and svc-lumen are expected to be missing; James Miller (finding 4) is not | Don't treat "Everyone" as all staff, and check that policies applied through it cover everyone they should |
| The contractor account `u-1005` is in "Everyone" | Check what "Everyone" grants, since it reaches a non-employee |
| User status arrives in mixed casing (`ACTIVE`, `active`) | None for us; worth knowing for other tools that read it |

### What we could not see yet

| Gap | Why | What it hides |
| --- | --- | --- |
| Audit log (logins, tool calls, OAuth grants) | Token lacks `audit.read` | What agents actually did, including whether payroll data left Acme |
| Who may approve connections | Not exposed by the API | Whether the contractor's Drive approval broke a rule |
| MFA for 4 accounts | Field not reported by Lumen | Whether those accounts, including 2 admins, are protected |
| Approver of 4 tools | No stored connection | Who is accountable for those paths |
| Meaning of status values and admin roles | Not documented | We assumed "active" = active, and "AI Platform Admins" = admins |

### What we need from you

1. **Today:** disable or contain Finance Copilot (finding 1).
2. Grant **`audit.read`** to the integration token, and confirm the token is read-only.
3. **Security team:** raise the vendor question in finding 3 with Lumen.
4. Assign an owner to Shadow Research Bot, or disable it, and decide on contractor-approved connections.
5. Review admin access for James Miller and svc-lumen, and enforce MFA.
6. Confirm our working definitions: admin groups, Acme domains, sensitive apps, and status values.

Once audit access is in place, we will re-run the collection and follow up with what the agents actually did.
