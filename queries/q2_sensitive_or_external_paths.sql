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

SELECT p.agent_id,
       p.agent_name                                                AS agent,
       COALESCE(p.owner_name || ' (' || p.owner_status || ')', 'no owner')  AS owner,
       CASE WHEN p.tool_kind = 'http' THEN p.target ELSE p.app END AS reaches,
       CASE WHEN p.is_sensitive = 1 THEN 'sensitive: ' || p.app
            ELSE 'external: ' || p.target_host END                 AS why,
       p.tool_scopes                                               AS scopes,
       CASE WHEN p.connection_id IS NULL THEN 'none on record (no stored connection)'
            WHEN p.authorizer_id IS NULL THEN 'none on record (connection has no authorizer)'
            WHEN p.authorizer_name IS NULL THEN p.authorizer_id || ' (not found in users)'
            ELSE p.authorizer_name || ' (' || p.authorizer_id || ', ' || p.authorizer_status
                 || CASE WHEN p.authorizer_is_internal = 0 THEN ', external: ' || p.authorizer_domain ELSE '' END
                 || ')' END                                        AS authorized_by,
       COALESCE(p.connection_last_used_at, '-')                    AS last_used_at,
       CASE WHEN EXISTS (SELECT 1 FROM v_agent_paths s
                          WHERE s.source_app = p.source_app AND s.agent_id = p.agent_id AND s.is_sensitive = 1)
             AND EXISTS (SELECT 1 FROM v_agent_paths x
                          WHERE x.source_app = p.source_app AND x.agent_id = p.agent_id AND x.is_external = 1)
            THEN 'YES' ELSE '' END                                 AS agent_combines
FROM v_agent_paths p
WHERE p.is_sensitive = 1 OR p.is_external = 1
ORDER BY agent_combines DESC, p.agent_id, p.tool_id;
