"""Study Abroad Atlas telemetry collector.

POST /v1/collect   batches of interaction events from the page (JSON, sent as text/plain so
                   browsers skip the CORS preflight; navigator.sendBeacon works too)
GET  /v1/stats     a quick engagement summary (Bearer ADMIN_TOKEN)
GET  /healthz      liveness + database check

Run: uvicorn app.main:create_app --factory
"""
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import ValidationError
from sqlalchemy import case, desc, func, select, text

from .config import Settings, get_settings
from .db import events, make_engine, save_batch, sessions, utcnow
from .schemas import Batch

BOT_RE = re.compile(r"bot|crawl|spider|slurp|headless|lighthouse|preview|facebookexternalhit|curl|wget|python-requests", re.I)


def parse_ua(ua: str) -> dict:
    """Coarse device / OS / browser, enough for BI splits. No fingerprinting."""
    if re.search(r"iPad|Tablet|PlayBook|Silk|Android(?!.*Mobile)", ua):
        device = "tablet"
    elif re.search(r"Mobi|iPhone|iPod|Android", ua):
        device = "mobile"
    else:
        device = "desktop"
    os_ = next((n for n, p in [("Android", r"Android"), ("iOS", r"iPhone|iPad|iPod"), ("Windows", r"Windows"),
                               ("ChromeOS", r"CrOS"), ("macOS", r"Mac OS X|Macintosh"), ("Linux", r"Linux")]
                if re.search(p, ua)), "Other")
    browser = next((n for n, p in [("Edge", r"Edg/"), ("Opera", r"OPR/|Opera"), ("Samsung", r"SamsungBrowser"),
                                   ("Firefox", r"Firefox|FxiOS"), ("Chrome", r"Chrome|CriOS"), ("Safari", r"Safari")]
                    if re.search(p, ua)), "Other")
    return {"device": device, "os": os_, "browser": browser, "is_bot": bool(BOT_RE.search(ua))}


class RateLimiter:
    """Fixed one-minute window per key, in memory. Good enough for one instance;
    put the limit in the load balancer if you run several."""

    def __init__(self, per_min: int):
        self.per_min, self.hits, self.lock = per_min, {}, threading.Lock()

    def allow(self, key: str) -> bool:
        window = int(time.time() // 60)
        with self.lock:
            if len(self.hits) > 50_000:
                self.hits.clear()
            w, n = self.hits.get(key, (window, 0))
            if w != window:
                w, n = window, 0
            self.hits[key] = (w, n + 1)
            return n < self.per_min


def to_dt(ms):
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).replace(tzinfo=None)
    except (OverflowError, OSError, ValueError):
        return None


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or get_settings()
    engine = make_engine(s.database_url)
    limiter = RateLimiter(s.rate_limit_per_min)
    allowed = set(s.allowed_origins)

    app = FastAPI(title="Study Abroad Atlas telemetry", version="1.0.0")
    app.state.engine = engine
    app.add_middleware(CORSMiddleware, allow_origins=list(allowed), allow_methods=["GET", "POST", "OPTIONS"],
                       allow_headers=["Content-Type", "Authorization"], max_age=86400)

    def client_ip(req: Request) -> str:
        if s.trust_proxy and (fwd := req.headers.get("x-forwarded-for")):
            return fwd.split(",")[0].strip()
        return req.client.host if req.client else "unknown"

    @app.get("/healthz")
    def healthz():
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {"ok": True}

    @app.post("/v1/collect", status_code=204)
    async def collect(req: Request):
        # sendBeacon and fetch always send Origin; the CORS middleware alone does not stop a
        # simple POST from reaching us, so the origin is checked here too.
        origin = (req.headers.get("origin") or "").rstrip("/")
        if origin not in allowed:
            raise HTTPException(403, "origin not allowed")
        if not limiter.allow(client_ip(req)):
            raise HTTPException(429, "slow down")
        if int(req.headers.get("content-length") or 0) > s.max_body_bytes:
            raise HTTPException(413, "batch too large")
        body = await req.body()
        if len(body) > s.max_body_bytes:
            raise HTTPException(413, "batch too large")
        try:
            batch = Batch.model_validate(json.loads(body))
        except (ValueError, ValidationError) as e:
            raise HTTPException(400, f"bad batch: {str(e)[:300]}")

        now = utcnow()
        ua = (req.headers.get("user-agent") or "")[:300]
        c = batch.ctx
        session_row = {
            "session_id": batch.sid, "visitor_id": batch.vid, "last_seen": now, "user_agent": ua, **parse_ua(ua),
            "landing_route": c and c.landing, "referrer": c and c.referrer,
            "utm_source": c and c.utm_source, "utm_medium": c and c.utm_medium, "utm_campaign": c and c.utm_campaign,
            "utm_content": c and c.utm_content, "utm_term": c and c.utm_term,
            "lang": c and c.lang, "tz": c and c.tz, "viewport_w": c and c.vw, "viewport_h": c and c.vh,
            "screen_w": c and c.sw, "screen_h": c and c.sh, "color_scheme": c and c.scheme,
            "is_returning": c and c.returning,
        }
        rows = [{
            "event_id": e.id, "session_id": batch.sid, "visitor_id": batch.vid, "seq": e.seq, "type": e.t,
            "client_ts": to_dt(e.ts), "received_at": now, "route": e.route, "view": e.view, "country": e.country,
            "section": e.section, "target": e.target, "value": e.value, "duration_ms": e.dur, "props": e.props,
        } for e in batch.events]
        save_batch(engine, session_row, rows)
        return Response(status_code=204)

    @app.get("/v1/stats")
    def stats(days: int = 30, authorization: str = Header("")):
        if not s.admin_token or authorization != f"Bearer {s.admin_token}":
            raise HTTPException(401, "bearer token required")
        since = utcnow() - timedelta(days=max(1, min(days, 365)))
        human = select(sessions.c.session_id).where(sessions.c.is_bot.is_(False), sessions.c.first_seen >= since)
        ev = lambda *cols: select(*cols).where(events.c.received_at >= since, events.c.session_id.in_(human))  # noqa: E731

        with engine.connect() as conn:
            def rows(q):
                return [dict(r._mapping) for r in conn.execute(q)]

            n = func.count().label("n")
            first = func.coalesce(events.c.value, "") != "cont"
            visits = lambda cond: func.sum(case((cond, 1), else_=0)).label("visits")  # noqa: E731
            secs = (func.coalesce(func.sum(events.c.duration_ms), 0) / 1000).label("seconds")
            return {
                "since": since.isoformat() + "Z",
                "sessions": conn.scalar(select(func.count()).select_from(human.subquery())),
                "visitors": conn.scalar(select(func.count(func.distinct(sessions.c.visitor_id)))
                                        .where(sessions.c.session_id.in_(human))),
                "events": conn.scalar(ev(func.count())),
                "daily_sessions": rows(select(func.date(sessions.c.first_seen).label("day"), n)
                                       .where(sessions.c.session_id.in_(human)).group_by("day").order_by("day")),
                # visits = route_enter; time = every route_leave, including the 'cont' records sent
                # after a tab was hidden and shown again, which are not separate visits
                "views": rows(ev(events.c.view, visits(events.c.type == "route_enter"), secs)
                              .where(events.c.type.in_(["route_enter", "route_leave"]))
                              .group_by(events.c.view).order_by(desc("seconds"))),
                "sections": rows(ev(events.c.view, events.c.section, visits(first), secs)
                                 .where(events.c.type == "section_view")
                                 .group_by(events.c.view, events.c.section).order_by(desc("seconds")).limit(40)),
                "countries": rows(ev(events.c.country, n).where(events.c.type == "route_enter",
                                                                events.c.country.is_not(None))
                                  .group_by(events.c.country).order_by(desc("n"))),
                "top_clicks": rows(ev(events.c.view, events.c.target, n).where(events.c.type == "click")
                                   .group_by(events.c.view, events.c.target).order_by(desc("n")).limit(30)),
                "devices": rows(select(sessions.c.device, n).where(sessions.c.session_id.in_(human))
                                .group_by(sessions.c.device).order_by(desc("n"))),
                "referrers": rows(select(sessions.c.utm_source, sessions.c.referrer, n)
                                  .where(sessions.c.session_id.in_(human))
                                  .group_by(sessions.c.utm_source, sessions.c.referrer).order_by(desc("n")).limit(20)),
                "recent_questions": [r.value for r in conn.execute(
                    ev(events.c.value).where(events.c.type == "chat_question")
                    .order_by(desc(events.c.received_at)).limit(25))],
            }

    return app
