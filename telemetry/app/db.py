"""Database schema and writes. SQLAlchemy Core, so the same code runs on SQLite and Postgres."""
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (JSON, BigInteger, Boolean, Column, DateTime, Index, Integer, MetaData, String, Table,
                        create_engine, select, update)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

metadata = MetaData()

# BIGINT autoincrement is not a thing in SQLite; INTEGER PRIMARY KEY is.
BigId = BigInteger().with_variant(Integer(), "sqlite")

# One row per browser tab session (sessionStorage), keyed by the id the page generates.
sessions = Table(
    "sessions", metadata,
    Column("session_id", String(64), primary_key=True),
    Column("visitor_id", String(64), nullable=False, index=True),
    Column("first_seen", DateTime, nullable=False, index=True),
    Column("last_seen", DateTime, nullable=False),
    Column("events_count", Integer, nullable=False, default=0),
    Column("landing_route", String(64)),
    Column("referrer", String(300)),
    Column("utm_source", String(100)),
    Column("utm_medium", String(100)),
    Column("utm_campaign", String(100)),
    Column("utm_content", String(100)),
    Column("utm_term", String(100)),
    Column("device", String(16)),
    Column("os", String(24)),
    Column("browser", String(24)),
    Column("lang", String(24)),
    Column("tz", String(64)),
    Column("viewport_w", Integer),
    Column("viewport_h", Integer),
    Column("screen_w", Integer),
    Column("screen_h", Integer),
    Column("color_scheme", String(8)),
    Column("is_returning", Boolean),
    Column("is_bot", Boolean, nullable=False, default=False),
    Column("user_agent", String(300)),
)

# One row per interaction.
events = Table(
    "events", metadata,
    Column("id", BigId, primary_key=True, autoincrement=True),
    Column("event_id", String(64), nullable=False, unique=True),  # client-made, so retries don't double count
    Column("session_id", String(64), nullable=False, index=True),
    Column("visitor_id", String(64), nullable=False, index=True),
    Column("seq", Integer),
    Column("type", String(40), nullable=False, index=True),
    Column("client_ts", DateTime),
    Column("received_at", DateTime, nullable=False, index=True),
    Column("route", String(64)),
    Column("view", String(32), index=True),
    Column("country", String(32), index=True),
    Column("section", String(64), index=True),
    Column("target", String(200)),
    Column("value", String(500)),
    Column("duration_ms", BigInteger),
    Column("props", JSON),
)
Index("ix_events_type_received", events.c.type, events.c.received_at)


def utcnow() -> datetime:
    """Naive UTC, so SQLite and Postgres store and compare the same values."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


BI_VIEWS = Path(__file__).resolve().parent.parent / "bi" / "views.sql"
log = logging.getLogger("atlas.telemetry")


def normalize_url(url: str) -> str:
    """Hosts hand out postgres:// or postgresql:// URLs; SQLAlchemy needs the driver named."""
    return re.sub(r"^postgres(ql)?://", "postgresql+psycopg://", url.strip())


def make_engine(url: str) -> Engine:
    url = normalize_url(url)
    # pre_ping: Neon and other serverless Postgres close idle connections while they sleep
    kw = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kw["connect_args"] = {"check_same_thread": False}
    engine = create_engine(url, **kw)
    metadata.create_all(engine)
    return engine


def load_bi_views(engine: Engine) -> bool:
    """Run bi/views.sql so BI tools find the reporting views without a manual step.
    A failure is logged, never fatal: collecting events matters more than the views."""
    if engine.dialect.name != "postgresql" or not BI_VIEWS.exists():
        return False
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql(BI_VIEWS.read_text(encoding="utf-8"))
        return True
    except Exception as e:  # noqa: BLE001
        log.warning("BI views not loaded: %s", e)
        return False


def save_batch(engine: Engine, session_row: dict, event_rows: list[dict]) -> int:
    """Upsert the session, insert events not seen before. Returns how many events were new."""
    now = session_row["last_seen"]
    with engine.begin() as conn:
        ids = [e["event_id"] for e in event_rows]
        seen = set(conn.scalars(select(events.c.event_id).where(events.c.event_id.in_(ids)))) if ids else set()
        fresh = [e for e in event_rows if e["event_id"] not in seen]
        if fresh:
            try:
                with conn.begin_nested():
                    conn.execute(events.insert(), fresh)
            except IntegrityError:  # a concurrent retry got there first; fall back to one by one
                inserted = []
                for e in fresh:
                    try:
                        with conn.begin_nested():
                            conn.execute(events.insert(), e)
                        inserted.append(e)
                    except IntegrityError:
                        pass
                fresh = inserted

        exists = conn.scalar(select(sessions.c.session_id).where(sessions.c.session_id == session_row["session_id"]))
        if exists:
            conn.execute(update(sessions).where(sessions.c.session_id == session_row["session_id"])
                         .values(last_seen=now, events_count=sessions.c.events_count + len(fresh)))
        else:
            conn.execute(sessions.insert(), {**session_row, "first_seen": now, "events_count": len(fresh)})
    return len(fresh)
