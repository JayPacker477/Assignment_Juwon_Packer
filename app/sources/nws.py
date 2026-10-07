"""National Weather Service (api.weather.gov): active alerts + text forecast per airport."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Optional

from .. import http
from ..models import Airport, Severity, Signal, SourceResult, SourceStatus, TripContext
from ..timeutil import overlaps, parse_iso

KEY = "nws"
NAME = "National Weather Service"
BASE = "https://api.weather.gov"
PUBLIC_URL = "https://www.weather.gov/"
HORIZON = "Alerts: active/upcoming; forecast: ~7 days"
FORECAST_MAX_DAYS = 6

# Ordered: first match wins. Severity reflects impact on *air travel*, not general public safety.
ALERT_RULES: list[tuple[str, int]] = [
    (r"blizzard|ice storm|hurricane warning|tropical storm warning|winter storm warning|tornado warning|"
     r"extreme wind|storm surge warning|dust storm|volcan", Severity.HIGH),
    (r"winter storm watch|winter weather advisory|hurricane watch|tropical storm watch|"
     r"severe thunderstorm|tornado watch|high wind|dense fog|freezing (rain|fog|drizzle)|lake effect snow|"
     r"snow squall|flash flood warning|heavy snow", Severity.MODERATE),
    (r"small craft|rip current|beach hazard|gale|marine|hazardous seas|surf|low water|"
     r"brisk wind|air quality|red flag|fire weather|frost|freeze", Severity.NONE),
    (r".", Severity.LOW),
]
CERTAINTY_CONF = {"Observed": "high", "Likely": "high", "Possible": "medium", "Unlikely": "low"}

FORECAST_RULES: list[tuple[str, int, str]] = [
    (r"freezing rain|freezing drizzle|sleet|ice accumulation|blizzard", Severity.HIGH, "ice/blizzard"),
    (r"heavy snow", Severity.HIGH, "heavy snow"),
    (r"severe thunderstorm", Severity.HIGH, "severe thunderstorms"),
    (r"\bsnow\b", Severity.MODERATE, "snow"),
    (r"thunderstorm", Severity.MODERATE, "thunderstorms"),
    (r"dense fog", Severity.MODERATE, "dense fog"),
    (r"\bfog\b", Severity.LOW, "fog"),
    (r"heavy rain", Severity.LOW, "heavy rain"),
]


def alert_severity(event: str) -> int:
    for pattern, sev in ALERT_RULES:
        if re.search(pattern, event, re.I):
            return int(sev)
    return int(Severity.LOW)


def forecast_severity(text: str, pop: Optional[int]) -> tuple[int, list[str]]:
    sev, reasons = Severity.NONE, []
    for pattern, s, label in FORECAST_RULES:
        if re.search(pattern, text, re.I):
            # "Slight chance" / low PoP mentions are downgraded one notch.
            if re.search(r"slight chance|isolated", text, re.I) or (pop is not None and pop < 30):
                s = max(s - 1, Severity.LOW)
            reasons.append(label)
            sev = max(sev, s)
    gusts = [int(g) for g in re.findall(r"gusts? (?:as high as |up to )?(\d+) mph", text, re.I)]
    if gusts and max(gusts) >= 50:
        sev, reasons = max(sev, Severity.MODERATE), reasons + [f"gusts {max(gusts)} mph"]
    elif gusts and max(gusts) >= 40:
        sev, reasons = max(sev, Severity.LOW), reasons + [f"gusts {max(gusts)} mph"]
    return int(sev), reasons


async def _alerts(a: Airport) -> list[dict]:
    data = await http.get_json(f"{BASE}/alerts/active", {"point": f"{a.lat},{a.lon}"}, ttl=180)
    return [f["properties"] for f in data.get("features", [])]


async def _forecast(a: Airport) -> Optional[dict]:
    pt = await http.get_json(f"{BASE}/points/{a.lat},{a.lon}", ttl=86400)
    url = pt["properties"].get("forecast")
    if not url:
        return None
    return await http.get_json(url, ttl=900)


def _alert_signals(alerts_by_airport: dict[str, list[dict]], ctx: TripContext) -> list[Signal]:
    merged: dict[str, tuple[dict, list[str]]] = {}
    for code, alerts in alerts_by_airport.items():
        for p in alerts:
            aid = p.get("id") or p.get("@id") or p.get("headline")
            merged.setdefault(aid, (p, []))[1].append(code)

    out = []
    for p, codes in merged.values():
        start = parse_iso(p.get("onset")) or parse_iso(p.get("effective"))
        end = parse_iso(p.get("ends")) or parse_iso(p.get("expires"))
        if not overlaps(start, end, ctx.window_start, ctx.window_end):
            continue
        event = p.get("event", "Alert")
        sev = alert_severity(event)
        conf = CERTAINTY_CONF.get(p.get("certainty", ""), "medium")
        out.append(
            Signal(
                source=KEY,
                source_name=NAME,
                location=",".join(codes),
                category="weather_alert",
                severity=sev,
                title=f"{event} ({p.get('severity', '?')} / {p.get('certainty', '?')})",
                detail=p.get("headline") or event,
                confidence=conf,
                relevance=(
                    "Alert period overlaps the trip date."
                    if sev
                    else "Overlaps the trip date but this alert type rarely affects air travel; not scored."
                ),
                counts_toward_score=sev > 0,
                valid_from=start,
                valid_to=end,
                issued_at=parse_iso(p.get("sent")),
                url=p.get("@id") or p.get("id"),
                raw=(p.get("description") or "")[:1200] or None,
            )
        )
    return out


def _forecast_signal(a: Airport, fc: dict, ctx: TripContext) -> Optional[Signal]:
    # NWS timestamps carry the local offset, so .date() is the airport-local date.
    periods = [
        p for p in fc.get("properties", {}).get("periods", [])
        if (s := parse_iso(p.get("startTime"))) and s.date() == ctx.travel_date
    ]
    if not periods:
        return None
    sev, reasons, lines = 0, [], []
    for p in periods:
        pop = (p.get("probabilityOfPrecipitation") or {}).get("value")
        s, r = forecast_severity(p.get("detailedForecast", ""), pop)
        sev, reasons = max(sev, s), reasons + r
        lines.append(f"{p['name']}: {p.get('detailedForecast', p.get('shortForecast', ''))}")
    conf = "high" if ctx.days_out <= 1 else "medium" if ctx.days_out <= 3 else "low"
    uniq = list(dict.fromkeys(reasons))
    return Signal(
        source=KEY,
        source_name=NAME,
        location=a.iata,
        category="forecast",
        severity=sev,
        title=("Forecast mentions " + ", ".join(uniq)) if uniq else "No travel-impacting weather in NWS forecast",
        detail=" | ".join(f"{p['name']}: {p.get('shortForecast', '')}" for p in periods),
        confidence=conf,
        relevance=f"NWS point forecast for {a.iata} on the trip date ({ctx.days_out} day(s) out).",
        valid_from=parse_iso(periods[0].get("startTime")),
        valid_to=parse_iso(periods[-1].get("endTime")),
        issued_at=parse_iso(fc.get("properties", {}).get("updateTime") or fc.get("properties", {}).get("updated")),
        url=f"https://forecast.weather.gov/MapClick.php?lat={a.lat}&lon={a.lon}",
        raw="\n".join(lines),
    )


async def fetch(ctx: TripContext) -> SourceResult:
    t0 = time.perf_counter()
    airports = ctx.all_airports
    alert_results = await asyncio.gather(*(_alerts(a) for a in airports), return_exceptions=True)
    errors = [r for r in alert_results if isinstance(r, Exception)]
    alerts_by_airport = {a.iata: r for a, r in zip(airports, alert_results) if not isinstance(r, Exception)}
    signals = _alert_signals(alerts_by_airport, ctx)

    fc_note = ""
    if ctx.days_out <= FORECAST_MAX_DAYS:
        primaries = [ctx.origin[0], ctx.destination[0]]
        fcs = await asyncio.gather(*(_forecast(a) for a in primaries), return_exceptions=True)
        for a, fc in zip(primaries, fcs):
            if isinstance(fc, Exception) or not fc:
                errors.append(fc if isinstance(fc, Exception) else RuntimeError("no forecast"))
                continue
            sig = _forecast_signal(a, fc, ctx)
            if sig:
                signals.append(sig)
    else:
        fc_note = f" Text forecast not available {ctx.days_out} days out (NWS horizon ~7 days)."

    if errors and not alerts_by_airport:
        raise RuntimeError(f"NWS unavailable: {errors[0]}")
    n_alerts = sum(1 for s in signals if s.category == "weather_alert")
    msg = f"{n_alerts} alert(s) overlapping trip date across {len(alerts_by_airport)} airport(s).{fc_note}"
    if errors:
        msg += f" {len(errors)} request(s) failed; results may be partial."
    return SourceResult(
        status=SourceStatus(
            key=KEY, name=NAME, status="ok", message=msg, horizon=HORIZON, url=PUBLIC_URL,
            records=len(signals), latency_ms=int((time.perf_counter() - t0) * 1000),
        ),
        signals=signals,
    )
