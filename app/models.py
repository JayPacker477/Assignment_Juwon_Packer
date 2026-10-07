from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import IntEnum
from typing import Any, Literal, Optional

from pydantic import BaseModel

Confidence = Literal["high", "medium", "low"]


class Severity(IntEnum):
    NONE = 0
    LOW = 1
    MODERATE = 2
    HIGH = 3


SEVERITY_LABEL = {0: "none", 1: "low", 2: "moderate", 3: "high"}


class Airport(BaseModel):
    iata: str
    icao: str
    name: str
    city: str
    state: str
    lat: float
    lon: float
    size: str
    tz: str


class Signal(BaseModel):
    """One normalized piece of evidence from one source about one location."""

    id: str = ""
    source: str
    source_name: str
    location: str  # airport IATA code, comma-joined codes, or "EN-ROUTE"
    category: str
    severity: int
    title: str
    detail: str
    confidence: Confidence
    relevance: str  # why this does / doesn't apply to the trip date
    counts_toward_score: bool = True
    valid_from: Optional[datetime] = None
    valid_to: Optional[datetime] = None
    issued_at: Optional[datetime] = None
    url: Optional[str] = None
    raw: Optional[str] = None


class SourceStatus(BaseModel):
    key: str
    name: str
    status: Literal["ok", "error", "not_applicable"]
    message: str
    horizon: str
    url: str
    records: int = 0
    latency_ms: Optional[int] = None


@dataclass
class TripContext:
    origin: list[Airport]  # primary first, then same-metro alternates
    destination: list[Airport]
    travel_date: date
    days_out: int
    window_start: datetime  # UTC
    window_end: datetime  # UTC
    now: datetime  # UTC

    @property
    def all_airports(self) -> list[Airport]:
        return self.origin + self.destination


@dataclass
class SourceResult:
    status: SourceStatus
    signals: list[Signal] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)
