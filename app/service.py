"""Orchestrates: resolve airports -> fetch all sources in parallel -> score -> AI briefing."""

from __future__ import annotations

import asyncio
import time
from datetime import date, datetime, time as dtime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from . import ai, airports, risk, sources
from .geo import great_circle_points, haversine_km
from .models import Airport, SourceResult, SourceStatus, TripContext

SOURCE_TIMEOUT_S = 20


class TripError(ValueError):
    pass


def _with_metro(primary: Airport, group: list[Airport]) -> list[Airport]:
    return [primary] + [a for a in group if a.iata != primary.iata]


async def resolve_side(query: str, airport_code: Optional[str]) -> tuple[list[Airport], Optional[str]]:
    group, note = await airports.resolve(query) if query else ([], None)
    if airport_code:
        chosen = airports.by_iata(airport_code)
        if not chosen:
            raise TripError(f"Unknown airport code '{airport_code}'.")
        if chosen.iata not in {a.iata for a in group}:
            group = airports.resolve_local(chosen.city) or []
        return _with_metro(chosen, group), note
    if not group:
        raise TripError(f"Couldn't find a U.S. airport for '{query}'. Try a city name or airport code.")
    return group, note


def build_context(origin: list[Airport], dest: list[Airport], travel_date: date, now: datetime) -> TripContext:
    o_tz, d_tz = ZoneInfo(origin[0].tz), ZoneInfo(dest[0].tz)
    today_local = now.astimezone(o_tz).date()
    days_out = (travel_date - today_local).days
    if days_out < 0:
        raise TripError("Travel date is in the past.")
    start = datetime.combine(travel_date, dtime.min, o_tz).astimezone(timezone.utc)
    end = datetime.combine(travel_date, dtime.max, d_tz).astimezone(timezone.utc)
    return TripContext(
        origin=origin, destination=dest, travel_date=travel_date, days_out=days_out,
        window_start=max(start, now), window_end=end, now=now,
    )


async def _run_source(mod, ctx: TripContext) -> SourceResult:
    t0 = time.perf_counter()
    try:
        return await asyncio.wait_for(mod.fetch(ctx), SOURCE_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 - one failing source must not sink the assessment
        msg = "timed out" if isinstance(e, asyncio.TimeoutError) else f"{type(e).__name__}: {str(e)[:160]}"
        return SourceResult(
            status=SourceStatus(
                key=mod.KEY, name=mod.NAME, status="error", horizon=mod.HORIZON, url=mod.PUBLIC_URL,
                message=f"Unavailable ({msg}). Its absence lowers confidence.",
                latency_ms=int((time.perf_counter() - t0) * 1000),
            )
        )


async def assess(
    origin: str,
    destination: str,
    travel_date: date,
    origin_airport: Optional[str] = None,
    destination_airport: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    t0 = time.perf_counter()
    now = now or datetime.now(timezone.utc)
    (o_group, o_note), (d_group, d_note) = await asyncio.gather(
        resolve_side(origin, origin_airport), resolve_side(destination, destination_airport)
    )
    if o_group[0].iata == d_group[0].iata:
        raise TripError("Origin and destination resolve to the same airport.")
    d_group = [a for a in d_group if a.iata not in {x.iata for x in o_group}]
    ctx = build_context(o_group, d_group, travel_date, now)

    results = await asyncio.gather(*(_run_source(m, ctx) for m in sources.ALL))
    statuses = [r.status for r in results]
    signals = risk.assign_ids([s for r in results for s in r.signals])
    scored = risk.score(ctx, signals, statuses)

    o, d = ctx.origin[0], ctx.destination[0]
    trip = {
        "origin_query": origin, "destination_query": destination,
        "origin_airport": o.iata, "destination_airport": d.iata,
        "origin_name": o.name, "destination_name": d.name,
        "date": travel_date.isoformat(), "days_out": ctx.days_out,
        "window_utc": [ctx.window_start.isoformat(), ctx.window_end.isoformat()],
        "distance_km": round(haversine_km(o.lat, o.lon, d.lat, d.lon)),
        "alternates_checked": [a.iata for a in ctx.origin[1:] + ctx.destination[1:]],
        "notes": [n for n in (o_note, d_note) if n],
    }
    signal_dicts = [s.model_dump(mode="json") for s in signals]
    status_dicts = [s.model_dump(mode="json") for s in statuses]
    briefing = await ai.write_briefing(trip, scored, signal_dicts, status_dicts)

    polygons = next((r.extras.get("polygons", []) for r in results if r.extras), [])
    return {
        "trip": trip,
        "assessment": scored,
        "briefing": briefing,
        "evidence": signal_dicts,
        "sources": status_dicts,
        "map": {
            "airports": [
                {"iata": a.iata, "name": a.name, "lat": a.lat, "lon": a.lon,
                 "role": "origin" if a is o else "destination" if a is d else "alternate"}
                for a in ctx.all_airports
            ],
            "route": great_circle_points(o.lat, o.lon, d.lat, d.lon, n=48),
            "polygons": polygons,
        },
        "generated_at": now.isoformat(),
        "elapsed_ms": int((time.perf_counter() - t0) * 1000),
    }
