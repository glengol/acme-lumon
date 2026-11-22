-- App-neutral schema for identity, agent and access data.
--
-- Every entity table is keyed on (source_app, external_id), so a second app
-- writes into the same tables with its own source_app value and no schema change.
-- Each row also records the `resource` that wrote it, so two resources of one
-- app can feed the same table (e.g. people and service accounts into identities)
-- and refreshing one never deletes the other's rows.
-- Connectors translate their own vocabulary into the neutral values noted on
-- each column (identity kind: human | service; tool kind: app_connection |
-- outbound_url), so the views and queries never mention any one app's terms.
--
-- Refresh rules:
--   * Snapshot resources (users, groups, agents, connections) are fetched
--     completely first, then their rows are replaced inside one transaction.
--   * The audit log (`events`) is append-only: new events are added, re-read
--     events land on the same row, nothing is deleted.
--   * A resource that fails is not touched: its previous rows stay, and
--     `coverage` records the failure.

-- ---------------------------------------------------------------- run tracking

CREATE TABLE IF NOT EXISTS sync_runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    source_app    TEXT NOT NULL,
    base_url      TEXT NOT NULL,
    started_at    TEXT NOT NULL,          -- UTC ISO-8601
    finished_at   TEXT,
    outcome       TEXT                    -- complete | partial | failed
);

-- One row per resource per run: what we got, and if not, why.
CREATE TABLE IF NOT EXISTS coverage (
    run_id        INTEGER NOT NULL REFERENCES sync_runs(run_id),
    resource      TEXT NOT NULL,          -- users | groups | agents | connections | audit
    status        TEXT NOT NULL,          -- ok | failed
    http_status   INTEGER,
    error         TEXT,                   -- API error code, or exception text
    detail        TEXT,                   -- extra context, e.g. required scopes
    records       INTEGER NOT NULL DEFAULT 0,
    pages         INTEGER,
    PRIMARY KEY (run_id, resource)
);

-- Every source object exactly as received, so nothing is lost by normalization.
CREATE TABLE IF NOT EXISTS raw_records (
    source_app    TEXT NOT NULL,
    resource      TEXT NOT NULL,
    external_id   TEXT NOT NULL,
    payload       TEXT NOT NULL,          -- JSON
    run_id        INTEGER NOT NULL,       -- run that wrote this row
    PRIMARY KEY (source_app, resource, external_id)
);

-- ---------------------------------------------------------------- entities

CREATE TABLE IF NOT EXISTS identities (
    source_app    TEXT NOT NULL,
    resource      TEXT NOT NULL,          -- which resource (endpoint) wrote this row
    external_id   TEXT NOT NULL,
    display_name  TEXT,
    login         TEXT,
    email         TEXT,
    email_domain  TEXT,                   -- lowercased domain of email, NULL if no email
    kind          TEXT,                   -- human | service | other value as received
    status_raw    TEXT,                   -- exactly as received (ACTIVE / active / ...)
    status        TEXT,                   -- lowercased status_raw
    mfa_enabled   INTEGER,                -- 1 / 0 / NULL (NULL = not reported = unknown)
    last_login_at TEXT,
    PRIMARY KEY (source_app, external_id)
);

CREATE TABLE IF NOT EXISTS groups (
    source_app    TEXT NOT NULL,
    resource      TEXT NOT NULL,          -- which resource (endpoint) wrote this row
    external_id   TEXT NOT NULL,
    name          TEXT,
    PRIMARY KEY (source_app, external_id)
);

-- A member is an identity ('user') or another group ('group').
CREATE TABLE IF NOT EXISTS group_members (
    source_app    TEXT NOT NULL,
    resource      TEXT NOT NULL,          -- which resource (endpoint) wrote this row
    group_id      TEXT NOT NULL,
    member_type   TEXT NOT NULL,          -- user | group
    member_id     TEXT NOT NULL,
    PRIMARY KEY (source_app, group_id, member_type, member_id)
);

CREATE TABLE IF NOT EXISTS agents (
    source_app    TEXT NOT NULL,
    resource      TEXT NOT NULL,          -- which resource (endpoint) wrote this row
    external_id   TEXT NOT NULL,
    name          TEXT,
    owner_id      TEXT,                   -- identity external_id, NULL if none
    status_raw    TEXT,
    status        TEXT,
    PRIMARY KEY (source_app, external_id)
);

-- What an agent can reach. One row per tool.
CREATE TABLE IF NOT EXISTS agent_tools (
    source_app    TEXT NOT NULL,
    resource      TEXT NOT NULL,          -- which resource (endpoint) wrote this row
    agent_id      TEXT NOT NULL,
    tool_id       TEXT NOT NULL,
    kind          TEXT,                   -- neutral: app_connection | outbound_url | other (as received)
    kind_raw      TEXT,                   -- the source app's own word, e.g. Lumen's saas_connector / http
    target        TEXT,                   -- app name, or URL for outbound_url
    target_host   TEXT,                   -- lowercased URL host for outbound_url, NULL otherwise
    connection_id TEXT,                   -- NULL when not backed by a stored connection
    scopes        TEXT,                   -- JSON array
    PRIMARY KEY (source_app, agent_id, tool_id)
);

-- Stored credentials an agent uses, and who approved them.
CREATE TABLE IF NOT EXISTS connections (
    source_app    TEXT NOT NULL,
    resource      TEXT NOT NULL,          -- which resource (endpoint) wrote this row
    external_id   TEXT NOT NULL,
    name          TEXT,
    app           TEXT,
    authorized_by TEXT,                   -- identity external_id
    scopes        TEXT,                   -- JSON array
    last_used_at  TEXT,
    PRIMARY KEY (source_app, external_id)
);

-- Activity log (logins, tool invocations, grants).
CREATE TABLE IF NOT EXISTS events (
    source_app    TEXT NOT NULL,
    resource      TEXT NOT NULL,          -- which resource (endpoint) wrote this row
    external_id   TEXT NOT NULL,
    occurred_at   TEXT,
    action        TEXT,
    actor_id      TEXT,
    target_id     TEXT,
    detail        TEXT,
    PRIMARY KEY (source_app, external_id)
);

-- ---------------------------------------------------------------- policy config
-- Reloaded from config.json on every run: customer decisions live in data, not code.

-- Apps explicitly classified. Any app NOT listed here is treated as sensitive.
CREATE TABLE IF NOT EXISTS app_classification (
    app           TEXT PRIMARY KEY,
    sensitive     INTEGER NOT NULL,       -- 1 / 0
    reason        TEXT
);

-- Customer-owned domains. Equal to one of these, or a subdomain of one = internal.
CREATE TABLE IF NOT EXISTS internal_domains (
    domain        TEXT PRIMARY KEY
);

-- Groups whose effective members (including nested groups) are admins.
CREATE TABLE IF NOT EXISTS admin_groups (
    source_app    TEXT NOT NULL,
    group_name    TEXT NOT NULL,
    PRIMARY KEY (source_app, group_name)
);
