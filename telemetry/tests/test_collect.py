import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import Settings
from app.db import events, metadata, sessions
from app.main import create_app, parse_ua

ORIGIN = "https://rajamnr175.github.io"
UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36"


@pytest.fixture
def client(tmp_path):
    # TEST_DATABASE_URL=postgresql+psycopg://... runs the same tests against Postgres (tables are dropped after).
    url = os.getenv("TEST_DATABASE_URL") or f"sqlite:///{tmp_path}/t.db"
    s = Settings(database_url=url, allowed_origins=[ORIGIN], admin_token="secret", rate_limit_per_min=5)
    app = create_app(s)
    with TestClient(app) as c:
        c.engine = app.state.engine
        yield c
    metadata.drop_all(app.state.engine)
    app.state.engine.dispose()


def ev(t="click", **kw):
    return {"id": uuid.uuid4().hex, "t": t, "ts": 1_793_000_000_000, "seq": 1, **kw}


def batch(*evs, sid="sess_abcdef12", vid="vis_abcdef12", **kw):
    return {"v": 1, "vid": vid, "sid": sid, "ctx": {"landing": "fit", "lang": "en-IN", "vw": 390, "returning": False},
            "events": list(evs), **kw}


def post(client, body, origin=ORIGIN, ua=UA):
    # The page sends text/plain to avoid a CORS preflight; mirror that.
    return client.post("/v1/collect", content=json.dumps(body),
                       headers={"Origin": origin, "Content-Type": "text/plain", "User-Agent": ua})


def all_rows(client, table):
    with client.engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(table))]


def test_stores_session_and_events(client):
    r = post(client, batch(ev("route_enter", route="usa", view="guide", country="usa"),
                           ev("click", view="guide", section="g-docs", target="#copyDocs", props={"x": 1})))
    assert r.status_code == 204
    s = all_rows(client, sessions)
    assert len(s) == 1 and s[0]["device"] == "mobile" and s[0]["os"] == "Android" and s[0]["browser"] == "Chrome"
    assert s[0]["events_count"] == 2 and s[0]["landing_route"] == "fit" and s[0]["is_bot"] is False
    e = all_rows(client, events)
    assert {x["type"] for x in e} == {"route_enter", "click"}
    assert next(x for x in e if x["type"] == "click")["props"] == {"x": 1}


def test_retry_does_not_double_count(client):
    b = batch(ev(), ev())
    post(client, b)
    post(client, b)
    assert len(all_rows(client, events)) == 2
    assert all_rows(client, sessions)[0]["events_count"] == 2


def test_rejects_unknown_origin(client):
    assert post(client, batch(ev()), origin="https://evil.example").status_code == 403
    assert post(client, batch(ev()), origin="").status_code == 403


def test_rejects_bad_batches(client):
    assert post(client, {"vid": "x", "sid": "y", "events": []}).status_code == 400
    assert post(client, batch(ev(t="DROP TABLE"))).status_code == 400
    assert client.post("/v1/collect", content="not json", headers={"Origin": ORIGIN}).status_code == 400
    assert post(client, batch(*[ev() for _ in range(101)])).status_code == 400


def test_long_values_are_cut_not_rejected(client):
    assert post(client, batch(ev(value="x" * 5000, target="y" * 999, props={"big": "z" * 5000}))).status_code == 204
    e = all_rows(client, events)[0]
    assert len(e["value"]) == 500 and len(e["target"]) == 200 and e["props"] == {"_dropped": "props over 2 KB"}


def test_body_size_limit(client):
    big = batch(*[ev(value="x" * 900) for _ in range(100)])
    assert post(client, big).status_code == 413


def test_rate_limit(client):
    codes = [post(client, batch(ev())).status_code for _ in range(7)]
    assert codes[:5] == [204] * 5 and codes[5:] == [429, 429]


def test_cors_preflight_and_headers(client):
    r = client.options("/v1/collect", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST",
                                               "Access-Control-Request-Headers": "content-type"})
    assert r.status_code == 200 and r.headers["access-control-allow-origin"] == ORIGIN


def test_bots_are_flagged_and_left_out_of_stats(client):
    post(client, batch(ev("route_enter", view="fit"), sid="botsess12"), ua="Googlebot/2.1")
    post(client, batch(ev("route_enter", view="guide", country="uk"),
                       ev("route_leave", view="guide", dur=12_000),
                       ev("route_leave", view="guide", dur=3_000, value="cont"),
                       ev("section_view", view="guide", section="g-cost", dur=4_000),
                       ev("chat_question", value="cost of uk")))
    assert {s["session_id"]: s["is_bot"] for s in all_rows(client, sessions)}["botsess12"] is True
    st = client.get("/v1/stats", headers={"Authorization": "Bearer secret"}).json()
    assert st["sessions"] == 1 and st["visitors"] == 1 and st["events"] == 5
    assert st["views"] == [{"view": "guide", "visits": 1, "seconds": 15}]
    assert st["sections"] == [{"view": "guide", "section": "g-cost", "visits": 1, "seconds": 4}]
    assert st["countries"] == [{"country": "uk", "n": 1}]
    assert st["recent_questions"] == ["cost of uk"]


def test_stats_needs_token(client):
    assert client.get("/v1/stats").status_code == 401
    assert client.get("/v1/stats", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_health(client):
    assert client.get("/healthz").json() == {"ok": True}


@pytest.mark.parametrize("ua,device,os_,browser", [
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1",
     "mobile", "iOS", "Safari"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36 Edg/130.0",
     "desktop", "Windows", "Edge"),
    ("Mozilla/5.0 (Linux; Android 14; SM-X710) AppleWebKit/537.36 Chrome/130.0 Safari/537.36", "tablet", "Android", "Chrome"),
])
def test_parse_ua(ua, device, os_, browser):
    p = parse_ua(ua)
    assert (p["device"], p["os"], p["browser"], p["is_bot"]) == (device, os_, browser, False)
