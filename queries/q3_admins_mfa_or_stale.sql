-- Q3. Which human admins are missing MFA or have not logged in for 30 days?
--
-- Admins (D7) are effective members of the configured admin groups, following
-- nested groups. MFA (D8): flagged unless confirmed on; "unknown" when Lumen
-- does not report it, which is NOT the same as "off".
-- Stale (D9): "today" is 2026-11-09 (given by the assignment), so stale means
-- no login since 2026-10-10, or no login recorded.

SELECT a.external_id                                          AS user_id,
       a.display_name                                         AS admin,
       a.admin_via                                            AS admin_via,
       CASE a.mfa_enabled WHEN 1 THEN 'on' WHEN 0 THEN 'off' ELSE 'unknown (not reported)' END AS mfa,
       COALESCE(substr(a.last_login_at, 1, 10), 'never')      AS last_login,
       CAST(julianday('2026-11-09') - julianday(substr(a.last_login_at, 1, 19)) AS INTEGER) AS days_since_login,
       trim(CASE WHEN a.mfa_enabled IS NOT 1 THEN 'MFA not confirmed; ' ELSE '' END ||
            CASE WHEN a.last_login_at IS NULL
                   OR julianday(substr(a.last_login_at, 1, 19)) < julianday('2026-11-09', '-30 days')
                 THEN 'no login in 30 days' ELSE '' END, '; ') AS reasons
FROM v_admins a
WHERE a.kind = 'human'
  AND a.is_active = 1
  AND (a.mfa_enabled IS NOT 1
       OR a.last_login_at IS NULL
       OR julianday(substr(a.last_login_at, 1, 19)) < julianday('2026-11-09', '-30 days'))
ORDER BY a.external_id;
