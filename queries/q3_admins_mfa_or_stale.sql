-- Q3. Which human admins are missing MFA or have not logged in for 30 days?
--
-- Admins (D7) are effective members of the configured admin groups, following
-- nested groups. Service accounts are excluded (the question is about humans);
-- an account whose type is unknown is kept, because it might be human.
-- MFA (D8): flagged unless confirmed on; "unknown" when Lumen does not report
-- it, which is NOT the same as "off".
-- Stale (D9): "today" is 2026-11-09 (given by the assignment). Days are counted
-- by calendar date. Stale = last login before 2026-10-10, none recorded, or a
-- value that is not a readable ISO-8601 timestamp (if in doubt, flag it).
-- Admins whose account is not active, or not found in users, are listed too:
-- holding admin rights in that state is itself a finding (D2).

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
