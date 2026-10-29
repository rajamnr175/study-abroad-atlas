-- Reporting views for BI tools (Metabase, Superset, Power BI, Looker Studio...). Postgres.
-- Load once after the collector has created its tables:  psql "$DATABASE_URL" -f bi/views.sql
-- Safe to re-run. Bot sessions are left out of every view.
--
-- Conventions in the events table:
--   route_enter   one per visit to a tab (view) or country guide (country)
--   route_leave   reading time for that visit in duration_ms (visible time only); value='cont' marks
--                 extra time after the tab was hidden and shown again, so SUM all of them for time
--                 but never count them as visits
--   section_view  a section was at least half on screen for 1s+; same 'cont' rule
--   click / input / toggle / chat_question / js_error / perf / page_view / session_start

CREATE OR REPLACE VIEW bi_sessions AS
SELECT s.session_id, s.visitor_id, s.first_seen, s.last_seen,
       EXTRACT(EPOCH FROM (s.last_seen - s.first_seen))::int AS span_seconds,
       s.events_count, s.landing_route, s.referrer,
       COALESCE(s.utm_source, CASE WHEN s.referrer IS NULL THEN 'direct' ELSE split_part(split_part(s.referrer, '://', 2), '/', 1) END) AS source,
       s.utm_medium, s.utm_campaign, s.device, s.os, s.browser, s.lang, s.tz, s.is_returning,
       (SELECT ROUND(COALESCE(SUM(e.duration_ms), 0) / 1000.0, 1) FROM events e
         WHERE e.session_id = s.session_id AND e.type = 'route_leave') AS reading_seconds,
       (SELECT COUNT(*) FROM events e WHERE e.session_id = s.session_id AND e.type = 'route_enter') AS pages
FROM sessions s
WHERE NOT s.is_bot;

-- Tabs: visits and reading time per day
CREATE OR REPLACE VIEW bi_view_daily AS
SELECT date_trunc('day', e.received_at)::date AS day, e.view,
       COUNT(*) FILTER (WHERE e.type = 'route_enter') AS visits,
       COUNT(DISTINCT e.session_id) AS sessions,
       ROUND(COALESCE(SUM(e.duration_ms) FILTER (WHERE e.type = 'route_leave'), 0) / 1000.0, 1) AS seconds
FROM events e JOIN sessions s USING (session_id)
WHERE NOT s.is_bot AND e.type IN ('route_enter', 'route_leave')
GROUP BY 1, 2;

-- Sections: which parts of each tab / country guide are actually read
CREATE OR REPLACE VIEW bi_section_daily AS
SELECT date_trunc('day', e.received_at)::date AS day, e.view, e.country, e.section,
       COUNT(*) FILTER (WHERE COALESCE(e.value, '') <> 'cont') AS views,
       COUNT(DISTINCT e.session_id) AS sessions,
       ROUND(SUM(e.duration_ms) / 1000.0, 1) AS seconds,
       ROUND(AVG(e.duration_ms) / 1000.0, 1) AS avg_seconds_per_record
FROM events e JOIN sessions s USING (session_id)
WHERE NOT s.is_bot AND e.type = 'section_view'
GROUP BY 1, 2, 3, 4;

-- Country interest: guide visits and reading time per country, split by traffic source
CREATE OR REPLACE VIEW bi_country_interest AS
SELECT date_trunc('day', e.received_at)::date AS day, e.country, b.source, b.device,
       COUNT(*) FILTER (WHERE e.type = 'route_enter') AS guide_visits,
       COUNT(DISTINCT e.session_id) AS sessions,
       COUNT(DISTINCT e.visitor_id) AS visitors,
       ROUND(COALESCE(SUM(e.duration_ms) FILTER (WHERE e.type = 'route_leave'), 0) / 1000.0, 1) AS seconds
FROM events e JOIN bi_sessions b USING (session_id)
WHERE e.view = 'guide' AND e.country IS NOT NULL AND e.type IN ('route_enter', 'route_leave')
GROUP BY 1, 2, 3, 4;

-- Clicks: every button / link / chip, where it sits and how often it is used
CREATE OR REPLACE VIEW bi_clicks AS
SELECT date_trunc('day', e.received_at)::date AS day, e.view, e.section, e.target,
       MIN(e.value) AS label, COUNT(*) AS clicks, COUNT(DISTINCT e.session_id) AS sessions,
       BOOL_OR((e.props ->> 'outbound')::boolean) AS outbound
FROM events e JOIN sessions s USING (session_id)
WHERE NOT s.is_bot AND e.type = 'click'
GROUP BY 1, 2, 3, 4;

-- Settled values people choose: budget, filters, searches, plan month/year, exchange rates
CREATE OR REPLACE VIEW bi_inputs AS
SELECT e.received_at, e.session_id, e.view, e.country, e.target AS field, e.value
FROM events e JOIN sessions s USING (session_id)
WHERE NOT s.is_bot AND e.type IN ('input', 'toggle');

-- What visitors ask the helper (emails and phone numbers already replaced on the page)
CREATE OR REPLACE VIEW bi_questions AS
SELECT e.received_at, e.session_id, e.view, e.value AS question,
       COALESCE((e.props ->> 'chip')::boolean, false) AS suggested
FROM events e JOIN sessions s USING (session_id)
WHERE NOT s.is_bot AND e.type = 'chat_question';

-- Funnel per session: budget board -> country guide -> checklist -> helper -> contact
CREATE OR REPLACE VIEW bi_funnel AS
SELECT b.session_id, b.first_seen::date AS day, b.source, b.device,
       BOOL_OR(e.type = 'route_enter' AND e.view = 'fit') AS saw_fit,
       BOOL_OR(e.type = 'input' AND e.target IN ('#budget', '#cmpBudget')) AS set_budget,
       BOOL_OR(e.type = 'route_enter' AND e.view = 'guide') AS opened_guide,
       BOOL_OR(e.type = 'toggle' AND e.section = 'g-docs') AS used_checklist,
       BOOL_OR(e.type = 'chat_question') AS asked_helper,
       BOOL_OR(e.type = 'click' AND e.target IN ('#contactGo', '#fabContact')) AS clicked_contact,
       BOOL_OR(e.type = 'contact_submit') AS sent_enquiry
FROM bi_sessions b LEFT JOIN events e USING (session_id)
GROUP BY 1, 2, 3, 4;

-- Front-end health
CREATE OR REPLACE VIEW bi_errors AS
SELECT date_trunc('day', e.received_at)::date AS day, e.value AS message,
       e.props ->> 'src' AS file, e.props ->> 'line' AS line, COUNT(*) AS n, COUNT(DISTINCT e.session_id) AS sessions
FROM events e
WHERE e.type = 'js_error'
GROUP BY 1, 2, 3, 4;

-- Enquiries without names, phone numbers, emails or messages: safe to share with a BI tool
CREATE OR REPLACE VIEW bi_leads_summary AS
SELECT created_at::date AS day, status, city, role, countries, level, field, intake, budget, stage, english,
       help, contact_via, page_view, page_country
FROM leads;
