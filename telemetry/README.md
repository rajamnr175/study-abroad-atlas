# Atlas telemetry

Records how visitors use the Study Abroad Atlas page (which tabs, country guides and sections they read and for how long, what they click, type and ask) and stores it in a database you can point BI tools at.

```
index.html (tracker)  --batches every 5 s, and on leave-->  POST /v1/collect  -->  Postgres (or SQLite)
                                                                                    |
                                                    Metabase / Superset / Power BI / SQL  <-- bi/views.sql
```

- **Tracker**: the last `<script>` in `../index.html`. It does nothing until `<meta name="atlas-telemetry">` holds your collector URL.
- **Collector**: `app/`, a small FastAPI service. It checks the Origin, validates and size-limits each batch, rate-limits per IP, drops duplicate retries and flags bots.
- **Database**: two tables, `sessions` and `events`. SQLite works for trying it out; use Postgres for anything real and for BI.

## What is recorded

| Event | When | Useful fields |
|---|---|---|
| `session_start` | first event of a new session (new tab, or 30 min idle) | `props.returning` |
| `page_view`, `perf` | page loaded; load timing | `props.load_ms`, `props.kb` |
| `route_enter` | a tab opened, or a country picked in the guide | `view`, `country` |
| `route_leave` | leaving that tab / country | `duration_ms` (visible time only), `props.scroll` (max % scrolled) |
| `section_view` | a section was at least half on screen for 1 s or more | `section`, `duration_ms` |
| `click` | any button, tab, chip or link | `target` (e.g. `#pri [data-p=cost]`), `value` (its label), `section`, `props.href`/`outbound` |
| `input` | a field settles on a value (budget, searches, filters, plan dates, exchange rates) | `target`, `value` |
| `toggle` | a checkbox (the document checklist) | `target`, `value` = on/off |
| `chat_question` | a question typed or picked in the helper | `value`, `props.chip` |
| `js_error` | a script error on the page | `value`, `props.src`, `props.line` |

Every event also carries `view`, `country`, `route`, the visitor's clock time (`client_ts`), its order in the session (`seq`) and the server's `received_at`. Each session row holds the landing tab, referrer, UTM tags, device / OS / browser, language, time zone, screen size and colour scheme.

Section names: `story` (header), `fitForm`, `fitSum`, `fitList`, `gHead`, `g-plan`, `g-steps`, `g-docs`, `g-unis`, `g-cost`, `g-rules`, `g-src`, `visionArt`, `compareChart`, `compareTable`, `procArt`, `pipe`, `consultTable`, `jobsArt`, `routes`, `agentSteps`, `contact`, `rates-box`, `cbPanel` (helper chat).

**Time records.** A `route_leave` or `section_view` with `value = 'cont'` is extra time after the visitor hid the tab and came back. Add those up for time spent, but don't count them as separate visits. The BI views already handle this.

## Privacy

- **Random ids only.** A visitor id lives in localStorage and a session id in sessionStorage. No cookies, no IP addresses stored (the IP is used in memory for rate limiting only) and no fingerprinting.
- **Typed text is cleaned.** Emails and phone numbers are replaced with `[email]` / `[number]` in the browser, before anything is sent.
- **Turned off** when the browser sends Do Not Track or Global Privacy Control. The footer shows a notice with a "Stop counting my visits" switch.
- **Your obligations.** If you add personal data later, or the audience grows, check what India's DPDP Act 2023 (and the GDPR, for EU visitors) asks of you, such as a privacy notice and retention limits. A retention job like the one below keeps the table small:

```sql
DELETE FROM events WHERE received_at < now() - interval '13 months';
DELETE FROM sessions WHERE last_seen < now() - interval '13 months';
```

## Run it locally

```bash
cd telemetry
pip install -r requirements-dev.txt
ADMIN_TOKEN=dev uvicorn app.main:create_app --factory --port 8001      # SQLite file telemetry.db
```

To try it with the page, serve the repo root on port 8000 (`python3 -m http.server 8000`). Then set the meta tag in a local copy of `index.html` to `http://127.0.0.1:8001/v1/collect`, open http://127.0.0.1:8000 and click around. Afterwards:

```bash
curl -H "Authorization: Bearer dev" http://127.0.0.1:8001/v1/stats
sqlite3 telemetry.db "select type, view, country, section, target, value, duration_ms from events order by id desc limit 20"
```

With Postgres, start everything with Docker:

```bash
cd telemetry
ADMIN_TOKEN=change-me POSTGRES_PASSWORD=change-me docker compose up -d --build
docker compose exec db psql -U atlas -d atlas -f /bi/views.sql
```

Tests run on SQLite by default. Set `TEST_DATABASE_URL` to also run them on Postgres, as CI does:

```bash
python -m pytest -q
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/atlas python -m pytest -q
```

## Deploy

GitHub Pages only serves static files, so the collector has to run somewhere else. Anything that runs a Docker container next to a Postgres database works: Render, Railway, Fly.io, Google Cloud Run with Cloud SQL, or a small VM with `docker compose`.

1. Create a Postgres database and copy its connection string.
2. Deploy this folder (it has a `Dockerfile`) with these settings:

   | Variable | Value |
   |---|---|
   | `DATABASE_URL` | `postgresql+psycopg://USER:PASSWORD@HOST:5432/DB` (change a `postgres://` prefix to `postgresql+psycopg://`) |
   | `ALLOWED_ORIGINS` | `https://rajamnr175.github.io` (comma-separate others; no trailing slash) |
   | `ADMIN_TOKEN` | a long random string, for `/v1/stats` |
   | `TRUST_PROXY` | `true` when behind the host's load balancer, so rate limits apply per visitor |
   | `RATE_LIMIT_PER_MIN` | batches per minute per IP, default 120 |
   | `PORT` | set by most hosts; defaults to 8080 |

3. Check `https://YOUR-COLLECTOR/healthz` returns `{"ok":true}`.
4. Load the BI views once: `psql "$DATABASE_URL" -f bi/views.sql`.
5. In `../index.html`, set `<meta name="atlas-telemetry" content="https://YOUR-COLLECTOR/v1/collect">`, then commit and push. Pages redeploys and events start arriving.

Free tiers that sleep when idle can lose the first batch while the service wakes up. Use an always-on instance if every visit matters.

## Business intelligence

`bi/views.sql` creates these Postgres views, all without bot traffic:

| View | Answers |
|---|---|
| `bi_sessions` | one row per visit: source (UTM or referrer host), device, reading time, pages |
| `bi_view_daily` | visits and reading time per tab per day |
| `bi_section_daily` | which sections of each tab and country guide are read, and for how long |
| `bi_country_interest` | guide visits and reading time per country, by source and device |
| `bi_clicks` | every control's use, with its label and whether it leaves the site |
| `bi_inputs` | budgets chosen, searches typed, filters set, checklist ticks |
| `bi_questions` | what people ask the helper |
| `bi_funnel` | per session: budget board, set a budget, opened a guide, used the checklist, asked the helper, clicked contact |
| `bi_errors` | front-end errors by message and line |

Point Metabase, Superset, Power BI or Looker Studio at the database with a read-only user, and build dashboards on these views. For example:

```sql
-- Which countries do WhatsApp visitors read most?
SELECT country, SUM(guide_visits) visits, SUM(seconds) seconds
FROM bi_country_interest WHERE source = 'whatsapp' GROUP BY 1 ORDER BY 2 DESC;

-- Funnel conversion for the last 30 days
SELECT COUNT(*) sessions, AVG(set_budget::int) set_budget, AVG(opened_guide::int) opened_guide,
       AVG(used_checklist::int) checklist, AVG(clicked_contact::int) contact
FROM bi_funnel WHERE day > current_date - 30;

-- Most common budgets set on the fit board
SELECT value AS lakh, COUNT(*) FROM bi_inputs WHERE field = '#budget' GROUP BY 1 ORDER BY 2 DESC;
```

`GET /v1/stats?days=30` with `Authorization: Bearer $ADMIN_TOKEN` returns a quick JSON summary (sessions, visitors, time per tab, top sections, countries, clicks, devices, sources, recent questions) without a BI tool.
