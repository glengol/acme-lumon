-- Q1. Which agents have no living owner?
--
-- A living owner (Decision D3) is an identity that exists, is active
-- (case-insensitive), and is human. Each agent that fails gets the reason.
-- Last-used time of the agent's connections shows whether it is still running.
-- Disabled agents are listed too (they can be re-enabled), but active ones sort first.

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
