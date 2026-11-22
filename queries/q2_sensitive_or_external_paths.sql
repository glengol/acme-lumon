-- Q2. Which agents can reach a sensitive system or send data outside Acme,
--     and who authorized that path?
--
-- One row per qualifying tool (Decisions D4, D5, D6):
--   sensitive = connector to an app not explicitly classified non-sensitive,
--               or any write/admin scope ("if in doubt, sensitive")
--   external  = http tool to a host outside the customer's domains
--   authorizer = the connection's authorized_by; "none on record" when the tool
--               has no stored connection. Never filled in with the owner.
-- `agent_combines` flags an agent that holds BOTH a sensitive read and an
-- external send: a path for sensitive data to leave the company.
-- Disabled agents are listed too (they can be re-enabled), but active ones sort first.

SELECT p.agent_id,
       p.agent_name                                                AS agent,
       COALESCE(p.agent_status, '-')                               AS agent_status,
       -- Every concatenated part is wrapped in COALESCE: in SQL, 'x' || NULL is NULL,
       -- and one missing field must never blank out the whole cell.
       CASE WHEN p.owner_id IS NULL  THEN 'no owner set'
            WHEN p.owner_ref IS NULL THEN p.owner_id || ' (not found in users)'
            ELSE COALESCE(p.owner_name, p.owner_id) || ' (' || COALESCE(p.owner_status, 'no status') || ')'
       END                                                         AS owner,
       COALESCE(CASE WHEN p.tool_kind = 'http' THEN p.target ELSE p.app END, '-') AS reaches,
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
