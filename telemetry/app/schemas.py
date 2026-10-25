"""The batch the page sends to POST /v1/collect. Over-long strings are cut, not rejected,
so one odd value never loses the rest of a batch."""
import json
from typing import Annotated, Any, Optional

from pydantic import BaseModel, BeforeValidator, Field, field_validator

ID = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")]


def _cut(n: int):
    def f(v: Any):
        if v is None:
            return None
        return str(v)[:n]
    return BeforeValidator(f)


Short = Annotated[Optional[str], _cut(64)]
Mid = Annotated[Optional[str], _cut(100)]
Long = Annotated[Optional[str], _cut(300)]

MAX_PROPS_BYTES = 2048


class Context(BaseModel):
    """Who and where: sent with every batch so a session row exists even if the first batch was lost."""
    landing: Short = None
    referrer: Long = None
    utm_source: Mid = None
    utm_medium: Mid = None
    utm_campaign: Mid = None
    utm_content: Mid = None
    utm_term: Mid = None
    lang: Annotated[Optional[str], _cut(24)] = None
    tz: Short = None
    vw: Optional[int] = Field(None, ge=0, le=20000)
    vh: Optional[int] = Field(None, ge=0, le=20000)
    sw: Optional[int] = Field(None, ge=0, le=20000)
    sh: Optional[int] = Field(None, ge=0, le=20000)
    scheme: Annotated[Optional[str], _cut(8)] = None
    returning: Optional[bool] = None


class Event(BaseModel):
    id: ID
    t: str = Field(pattern=r"^[a-z][a-z_]{1,39}$")
    ts: Optional[int] = Field(None, ge=0)  # epoch ms on the visitor's clock
    seq: Optional[int] = Field(None, ge=0, le=10_000_000)
    route: Short = None
    view: Annotated[Optional[str], _cut(32)] = None
    country: Annotated[Optional[str], _cut(32)] = None
    section: Short = None
    target: Annotated[Optional[str], _cut(200)] = None
    value: Annotated[Optional[str], _cut(500)] = None
    dur: Optional[int] = Field(None, ge=0, le=86_400_000)
    props: Optional[dict[str, Any]] = None

    @field_validator("props")
    @classmethod
    def small_props(cls, v):
        if v is not None and len(json.dumps(v, default=str)) > MAX_PROPS_BYTES:
            return {"_dropped": "props over 2 KB"}
        return v


class Batch(BaseModel):
    v: int = 1
    vid: ID
    sid: ID
    ctx: Optional[Context] = None
    events: list[Event] = Field(min_length=1, max_length=100)
