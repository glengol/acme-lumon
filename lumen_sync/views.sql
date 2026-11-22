-- Analysis views. App-neutral: they read only the shared tables and the policy
-- config tables, never anything Lumen-specific. Recreated on every run.

DROP VIEW IF EXISTS v_identities;
DROP VIEW IF EXISTS v_admins;
DROP VIEW IF EXISTS v_admin_groups_missing;
DROP VIEW IF EXISTS v_admin_warnings;
DROP VIEW IF EXISTS v_admin_tree;
DROP VIEW IF EXISTS v_agent_paths;

-- Identities with the derived flags every question needs.
--   is_active:   status (already lowercased) is exactly 'active'. Anything else,
--                including an unexpected or missing value, counts as NOT active.
--   is_internal: email domain is a customer domain or a subdomain of one
--                (exact suffix compare, not LIKE, where '_' would be a wildcard).
--                NULL when there is no email (cannot tell).
CREATE VIEW v_identities AS
SELECT i.*,
       CASE WHEN i.status = 'active' THEN 1 ELSE 0 END AS is_active,
       CASE WHEN i.email_domain IS NULL THEN NULL
            WHEN EXISTS (SELECT 1 FROM internal_domains d
                         WHERE i.email_domain = d.domain
                            OR substr(i.email_domain, -length(d.domain) - 1) = '.' || d.domain) THEN 1
            ELSE 0 END AS is_internal
FROM identities i;

-- Admins = effective user members of the configured admin groups, following
-- nested groups to any depth.
--   * A configured entry matches a group by id, or by name ignoring case and
--     surrounding spaces, so "AI Platform Admins " still matches.
--   * `path_ids` stops cycles (a group is never visited twice on one path).
--   * `admin_via` is the shortest path by which the user became an admin.
-- v_admin_tree: every group reached from a configured admin group (group_found
-- is 0 for a nested group id that does not exist).
CREATE VIEW v_admin_tree AS
WITH RECURSIVE admin_tree (source_app, group_id, via, path_ids, depth, group_found) AS (
    SELECT g.source_app, g.external_id, g.name, '/' || g.external_id || '/', 0, 1
    FROM groups g
    JOIN admin_groups a ON a.source_app = g.source_app
                       AND (g.external_id = trim(a.group_name)
                            OR lower(trim(g.name)) = lower(trim(a.group_name)))
    UNION
    SELECT m.source_app, m.member_id, t.via || ' > ' || COALESCE(g.name, m.member_id),
           t.path_ids || m.member_id || '/', t.depth + 1, g.external_id IS NOT NULL
    FROM admin_tree t
    JOIN group_members m ON m.source_app = t.source_app AND m.group_id = t.group_id AND m.member_type = 'group'
    LEFT JOIN groups g ON g.source_app = m.source_app AND g.external_id = m.member_id
    WHERE instr(t.path_ids, '/' || m.member_id || '/') = 0
)
SELECT * FROM admin_tree;

-- Anything that could make the admin list silently incomplete or too broad.
-- The report prints each row as a WARNING.
CREATE VIEW v_admin_warnings AS
SELECT a.source_app, 'config entry "' || a.group_name || '" matches no group by id or name' AS warning
FROM admin_groups a
WHERE NOT EXISTS (SELECT 1 FROM groups g WHERE g.source_app = a.source_app
                    AND (g.external_id = trim(a.group_name) OR lower(trim(g.name)) = lower(trim(a.group_name))))
UNION ALL
SELECT a.source_app, 'config entry "' || a.group_name || '" matches ' || COUNT(*) || ' groups ('
       || group_concat(g.external_id || ' "' || g.name || '"', ', ') || '); all are treated as admin groups'
FROM admin_groups a
JOIN groups g ON g.source_app = a.source_app
             AND (g.external_id = trim(a.group_name) OR lower(trim(g.name)) = lower(trim(a.group_name)))
GROUP BY a.source_app, a.group_name
HAVING COUNT(*) > 1
UNION ALL
SELECT t.source_app, 'nested group ' || t.group_id || ' (via ' || t.via || ') does not exist; its members cannot be checked'
FROM v_admin_tree t WHERE t.group_found = 0
UNION ALL
SELECT m.source_app, 'member ' || m.member_id || ' of admin group ' || m.group_id
       || ' has unknown type "' || m.member_type || '"; not counted as an admin'
FROM v_admin_tree t
JOIN group_members m ON m.source_app = t.source_app AND m.group_id = t.group_id
WHERE m.member_type NOT IN ('user', 'group');

-- Kept for compatibility: configured entries that matched no group.
CREATE VIEW v_admin_groups_missing AS
SELECT a.source_app, a.group_name
FROM admin_groups a
WHERE NOT EXISTS (SELECT 1 FROM groups g
                  WHERE g.source_app = a.source_app
                    AND (g.external_id = trim(a.group_name)
                         OR lower(trim(g.name)) = lower(trim(a.group_name))));

CREATE VIEW v_admins AS
WITH paths AS (
    SELECT m.source_app, m.member_id AS user_id, t.via,
           ROW_NUMBER() OVER (PARTITION BY m.source_app, m.member_id ORDER BY t.depth, t.via) AS n
    FROM v_admin_tree t
    JOIN group_members m ON m.source_app = t.source_app AND m.group_id = t.group_id AND m.member_type = 'user'
)
-- LEFT JOIN: a member id that is not a known user is kept (external_id NULL),
-- so it surfaces in the answers instead of being silently dropped.
SELECT p.user_id AS admin_id, p.source_app AS admin_source_app, i.*, p.via AS admin_via
FROM paths p
LEFT JOIN v_identities i ON i.source_app = p.source_app AND i.external_id = p.user_id
WHERE p.n = 1;

-- One row per agent tool: what it reaches, whether that is sensitive or
-- external, and who (if anyone) is on record as authorizing it.
--   app:          the connection's app, else the connector target. NULL for outbound_url.
--   is_sensitive: connector to an app NOT explicitly classified as non-sensitive
--                 ("if in doubt, sensitive"), or any single scope (checked one by
--                 one with json_each) that grants powerful access: '*', or one
--                 containing write, admin, delete, manage, full, owner or root.
--   is_external:  outbound_url tool whose host is not a customer domain or subdomain.
CREATE VIEW v_agent_paths AS
WITH base AS (
    SELECT t.source_app,
           a.external_id   AS agent_id,
           a.name          AS agent_name,
           a.status        AS agent_status,
           a.owner_id,
           o.external_id   AS owner_ref,       -- NULL when owner_id does not resolve to a user
           o.display_name  AS owner_name,
           o.status        AS owner_status,
           o.status_raw    AS owner_status_raw,
           o.kind          AS owner_kind,
           t.tool_id,
           t.kind          AS tool_kind,
           t.target,
           t.target_host,
           t.scopes        AS tool_scopes,
           -- unknown kind counts as a connector; the tool's own target is what it says it reaches
           CASE WHEN t.kind = 'outbound_url' THEN NULL ELSE lower(COALESCE(t.target, c.app)) END AS app,
           CASE WHEN t.kind = 'outbound_url' THEN NULL ELSE lower(c.app) END AS connection_app,
           t.connection_id,
           c.external_id   AS connection_ref,   -- NULL when connection_id does not resolve
           c.name          AS connection_name,
           c.scopes        AS connection_scopes,
           c.last_used_at  AS connection_last_used_at,
           c.authorized_by AS authorizer_id,
           au.external_id  AS authorizer_ref,  -- NULL when authorized_by does not resolve to a user
           au.display_name AS authorizer_name,
           au.status       AS authorizer_status,
           au.status_raw   AS authorizer_status_raw,
           au.email_domain AS authorizer_domain,
           au.is_internal  AS authorizer_is_internal
    FROM agent_tools t
    JOIN agents a             ON a.source_app = t.source_app AND a.external_id = t.agent_id
    LEFT JOIN v_identities o  ON o.source_app = a.source_app AND o.external_id = a.owner_id
    LEFT JOIN connections c   ON c.source_app = t.source_app AND c.external_id = t.connection_id
    LEFT JOIN v_identities au ON au.source_app = c.source_app AND au.external_id = c.authorized_by
)
SELECT b.*,
       -- When the tool's target and its connection's app disagree, either one being
       -- sensitive is enough ("if in doubt"); app_mismatch flags the disagreement.
       CASE WHEN COALESCE(b.tool_kind, '') <> 'outbound_url' AND (
                 NOT EXISTS (SELECT 1 FROM app_classification ac WHERE ac.app = b.app AND ac.sensitive = 0)
                 OR (b.connection_app IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM app_classification ac WHERE ac.app = b.connection_app AND ac.sensitive = 0))
                 OR EXISTS (SELECT 1 FROM json_each(b.tool_scopes) s WHERE lower(s.value) = '*' OR lower(s.value) LIKE '%write%' OR lower(s.value) LIKE '%admin%' OR lower(s.value) LIKE '%delete%' OR lower(s.value) LIKE '%manage%' OR lower(s.value) LIKE '%full%' OR lower(s.value) LIKE '%owner%' OR lower(s.value) LIKE '%root%')
                 OR EXISTS (SELECT 1 FROM json_each(b.connection_scopes) s WHERE lower(s.value) = '*' OR lower(s.value) LIKE '%write%' OR lower(s.value) LIKE '%admin%' OR lower(s.value) LIKE '%delete%' OR lower(s.value) LIKE '%manage%' OR lower(s.value) LIKE '%full%' OR lower(s.value) LIKE '%owner%' OR lower(s.value) LIKE '%root%'))
            THEN 1 ELSE 0 END AS is_sensitive,
       CASE WHEN b.connection_app IS NOT NULL AND b.app IS NOT NULL AND b.connection_app <> b.app
            THEN 1 ELSE 0 END AS app_mismatch,
       CASE WHEN b.tool_kind = 'outbound_url' AND (b.target_host IS NULL OR NOT EXISTS (
                 SELECT 1 FROM internal_domains d
                 WHERE b.target_host = d.domain OR substr(b.target_host, -length(d.domain) - 1) = '.' || d.domain))
            THEN 1 ELSE 0 END AS is_external
FROM base b;

-- One row per person (by email) across all apps, so a question like "agents
-- owned by people who have left" can follow the same person from one platform
-- to another. Accounts without an email (usually service accounts) are left out.
DROP VIEW IF EXISTS v_person_accounts;
CREATE VIEW v_person_accounts AS
SELECT lower(email)                                   AS person,
       COUNT(*)                                       AS accounts,
       COUNT(DISTINCT source_app)                     AS apps,
       group_concat(source_app || ':' || external_id || ' (' || COALESCE(status, 'no status') || ')', ', ')
                                                      AS details,
       MAX(is_active)                                 AS active_somewhere,
       MIN(is_active)                                 AS active_everywhere
FROM v_identities
WHERE email IS NOT NULL
GROUP BY lower(email);
