"""AviationWeather.gov SIGMETs/AIRMETs intersected with the great-circle flight path."""

from __future__ import annotations

import time

from .. import http
from ..geo import great_circle_points, path_intersects_polygon
from ..models import Severity, Signal, SourceResult, SourceStatus, TripContext
from ..timeutil import fmt_utc, from_epoch, overlaps

KEY = "sigmet"
NAME = "AviationWeather.gov SIGMETs (en-route)"
URL = "https://aviationweather.gov/api/data/airsigmet"
PUBLIC_URL = "https://aviationweather.gov/gfa/"
HORIZON = "Next ~2–6 hours"

HAZARD_SEVERITY = {"CONVECTIVE": Severity.MODERATE, "ASH": Severity.HIGH, "TURB": Severity.LOW, "ICE": Severity.LOW}


async def fetch(ctx: TripContext) -> SourceResult:
    t0 = time.perf_counter()
    if ctx.days_out > 0:
        return SourceResult(
            status=SourceStatus(
                key=KEY, name=NAME, status="not_applicable", horizon=HORIZON, url=PUBLIC_URL,
                message="SIGMETs are valid for a few hours; only used for same-day trips.",
            )
        )
    o, d = ctx.origin[0], ctx.destination[0]
    path = great_circle_points(o.lat, o.lon, d.lat, d.lon, n=96)
    data = await http.get_json(URL, {"format": "json"}, ttl=300)

    signals, polygons = [], []
    for s in data:
        hazard = (s.get("hazard") or "").upper()
        if s.get("airSigmetType") != "SIGMET" or hazard not in HAZARD_SEVERITY:
            continue
        poly = [(c["lat"], c["lon"]) for c in s.get("coords") or [] if "lat" in c and "lon" in c]
        start, end = from_epoch(s.get("validTimeFrom")), from_epoch(s.get("validTimeTo"))
        if not overlaps(start, end, ctx.window_start, ctx.window_end):
            continue
        if not path_intersects_polygon(path, poly):
            continue
        polygons.append({"hazard": hazard, "coords": poly})
        alt = s.get("altitudeHi1")
        signals.append(
            Signal(
                source=KEY,
                source_name=NAME,
                location="EN-ROUTE",
                category="enroute_hazard",
                severity=int(HAZARD_SEVERITY[hazard]),
                title=f"{hazard.title()} SIGMET crosses the flight path" + (f" (tops FL{alt // 100})" if alt else ""),
                detail=f"Valid {fmt_utc(start)}–{fmt_utc(end)}. Airlines typically reroute around these; "
                       "expect possible reroutes, longer flight times, or turbulence rather than cancellation.",
                confidence="medium",
                relevance="Active now and intersecting the great-circle route; short-lived (hours).",
                valid_from=start,
                valid_to=end,
                url=PUBLIC_URL,
                raw=(s.get("rawAirSigmet") or "").strip() or None,
            )
        )
    return SourceResult(
        status=SourceStatus(
            key=KEY, name=NAME, status="ok", horizon=HORIZON, url=PUBLIC_URL, records=len(signals),
            message=f"{len(data)} active SIGMET/AIRMETs nationally; {len(signals)} intersect your route.",
            latency_ms=int((time.perf_counter() - t0) * 1000),
        ),
        signals=signals,
        extras={"route": path, "polygons": polygons},
    )
