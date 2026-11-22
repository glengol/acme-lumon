-- Analysis views. App-neutral: they read only the shared tables and the policy
-- config tables, never anything Lumen-specific. Recreated on every run.

DROP VIEW IF EXISTS v_identities;
DROP VIEW IF EXISTS v_admins;
DROP VIEW IF EXISTS v_agent_paths;

-- Identities with the derived flags every question needs.
--   is_active:   status (already lowercased) is exactly 'active'. Anything else,
--                including an unexpected or missing value, counts as NOT active.
--   is_internal: email domain is a customer domain or a subdomain of one.
--                NULL when there is no email (cannot tell).
CREATE VIEW v_identities AS
SELECT i.*,
       CASE WHEN i.status = 'active' THEN 1 ELSE 0 END AS is_active,
       CASE WHEN i.email_domain IS NULL THEN NULL
            WHEN EXISTS (SELECT 1 FROM internal_domains d
                         WHERE i.email_domain = d.domain
                            OR i.email_domain LIKE '%.' || d.domain) THEN 1
            ELSE 0 END AS is_internal
FROM identities i;

-- Admins = effective user members of the configured admin groups, following
-- nested groups to any depth. `path_ids` stops cycles (a group is never
-- visited twice on the same path). `via` shows how the user became an admin.
CREATE VIEW v_admins AS
WITH RECURSIVE admin_tree (source_app, group_id, via, path_ids) AS (
    SELECT g.source_app, g.external_id, g.name, '/' || g.external_id || '/'
    FROM groups g
    JOIN admin_groups a ON a.source_app = g.source_app AND a.group_name = g.name
    UNION
    SELECT m.source_app, m.member_id, t.via || ' > ' || COALESCE(g.name, m.member_id),
           t.path_ids || m.member_id || '/'
    FROM admin_tree t
    JOIN group_members m ON m.source_app = t.source_app AND m.group_id = t.group_id AND m.member_type = 'group'
    LEFT JOIN groups g ON g.source_app = m.source_app AND g.external_id = m.member_id
    WHERE instr(t.path_ids, '/' || m.member_id || '/') = 0
)
SELECT i.*, MIN(t.via) AS admin_via
FROM admin_tree t
JOIN group_members m ON m.source_app = t.source_app AND m.group_id = t.group_id AND m.member_type = 'user'
JOIN v_identities i ON i.source_app = m.source_app AND i.external_id = m.member_id
GROUP BY i.source_app, i.external_id;

-- One row per agent tool: what it reaches, whether that is sensitive or
-- external, and who (if anyone) is on record as authorizing it.
--   app:          the connection's app, else the connector target. NULL for http.
--   is_sensitive: connector to an app NOT explicitly classified as non-sensitive
--                 ("if in doubt, sensitive"), or any write/admin scope.
--   is_external:  http tool whose host is not a customer domain or subdomain.
CREATE VIEW v_agent_paths AS
WITH base AS (
    SELECT t.source_app,
           a.external_id   AS agent_id,
           a.name          AS agent_name,
           a.status        AS agent_status,
           a.owner_id,
           o.display_name  AS owner_name,
           o.status        AS owner_status,
           o.kind          AS owner_kind,
           t.tool_id,
           t.kind          AS tool_kind,
           t.target,
           t.target_host,
           t.scopes        AS tool_scopes,
           CASE WHEN t.kind = 'http' THEN NULL ELSE lower(COALESCE(c.app, t.target)) END AS app,
           t.connection_id,
           c.name          AS connection_name,
           c.scopes        AS connection_scopes,
           c.last_used_at  AS connection_last_used_at,
           c.authorized_by AS authorizer_id,
           au.display_name AS authorizer_name,
           au.status       AS authorizer_status,
           au.email_domain AS authorizer_domain,
           au.is_internal  AS authorizer_is_internal
    FROM agent_tools t
    JOIN agents a             ON a.source_app = t.source_app AND a.external_id = t.agent_id
    LEFT JOIN v_identities o  ON o.source_app = a.source_app AND o.external_id = a.owner_id
    LEFT JOIN connections c   ON c.source_app = t.source_app AND c.external_id = t.connection_id
    LEFT JOIN v_identities au ON au.source_app = c.source_app AND au.external_id = c.authorized_by
)
SELECT b.*,
       CASE WHEN b.tool_kind <> 'http' AND (
                 NOT EXISTS (SELECT 1 FROM app_classification ac WHERE ac.app = b.app AND ac.sensitive = 0)
                 OR lower(b.tool_scopes) LIKE '%write%' OR lower(b.tool_scopes) LIKE '%admin%'
                 OR lower(COALESCE(b.connection_scopes, '')) LIKE '%write%'
                 OR lower(COALESCE(b.connection_scopes, '')) LIKE '%admin%')
            THEN 1 ELSE 0 END AS is_sensitive,
       CASE WHEN b.tool_kind = 'http' AND (b.target_host IS NULL OR NOT EXISTS (
                 SELECT 1 FROM internal_domains d
                 WHERE b.target_host = d.domain OR b.target_host LIKE '%.' || d.domain))
            THEN 1 ELSE 0 END AS is_external
FROM base b;
