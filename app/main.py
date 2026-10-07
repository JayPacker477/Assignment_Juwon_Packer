from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import ai, airports, config, http
from .service import TripError, assess

STATIC = config.ROOT / "static"
# Relative dates ("tomorrow") are resolved in U.S. Eastern unless the user picks a date.
DEFAULT_TZ = ZoneInfo("America/New_York")


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await http.close()


app = FastAPI(title="Travel Disruption Radar", lifespan=lifespan)


class ParseRequest(BaseModel):
    text: str = Field(min_length=3, max_length=300)


class AssessRequest(BaseModel):
    origin: str = Field(default="", max_length=80)
    destination: str = Field(default="", max_length=80)
    date: date
    origin_airport: Optional[str] = Field(default=None, max_length=4)
    destination_airport: Optional[str] = Field(default=None, max_length=4)


@app.get("/api/health")
async def health():
    return {"ok": True, "ai_enabled": ai.enabled(), "model": config.ANTHROPIC_MODEL if ai.enabled() else None}


@app.get("/api/airports")
async def airport_search(q: str = ""):
    return [a.model_dump() for a in airports.search(q)]


@app.post("/api/parse")
async def parse(req: ParseRequest):
    today = datetime.now(timezone.utc).astimezone(DEFAULT_TZ).date()
    try:
        return await ai.parse_trip(req.text, today)
    except ValueError as e:
        raise HTTPException(422, str(e)) from e


@app.post("/api/assess")
async def assess_trip(req: AssessRequest):
    if not (req.origin or req.origin_airport) or not (req.destination or req.destination_airport):
        raise HTTPException(422, "Origin and destination are required.")
    try:
        return await assess(req.origin, req.destination, req.date, req.origin_airport, req.destination_airport)
    except TripError as e:
        raise HTTPException(422, str(e)) from e


@app.get("/")
async def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
