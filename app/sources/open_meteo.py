"""Open-Meteo numerical weather forecast: the only free source reaching ~16 days out."""

from __future__ import annotations

import time

from .. import http
from ..models import Severity, Signal, SourceResult, SourceStatus, TripContext

KEY = "open_meteo"
NAME = "Open-Meteo Forecast"
URL = "https://api.open-meteo.com/v1/forecast"
PUBLIC_URL = "https://open-meteo.com/"
HORIZON = "Up to 16 days (confidence decays with lead time)"
MAX_DAYS_OUT = 15

WMO = {
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle", 55: "dense drizzle",
    56: "freezing drizzle", 57: "heavy freezing drizzle", 61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
    77: "snow grains", 80: "rain showers", 81: "heavy rain showers", 82: "violent rain showers",
    85: "snow showers", 86: "heavy snow showers", 95: "thunderstorms", 96: "thunderstorms w/ hail",
    99: "severe thunderstorms w/ hail",
}
CODE_SEVERITY = {
    56: 3, 57: 3, 66: 3, 67: 3, 75: 3, 86: 3, 96: 3, 99: 3,
    71: 1, 73: 2, 77: 1, 85: 2, 95: 2, 82: 2, 48: 1, 45: 1, 65: 1,
}
DAILY = "weather_code,precipitation_sum,snowfall_sum,wind_gusts_10m_max,precipitation_probability_max"


def assess_day(d: dict) -> tuple[int, list[str]]:
    sev, why = Severity.NONE, []
    code = d.get("weather_code")
    if code in CODE_SEVERITY:
        sev, why = max(sev, CODE_SEVERITY[code]), why + [WMO.get(code, f"code {code}")]
    snow = d.get("snowfall_sum") or 0
    if snow >= 10:
        sev, why = max(sev, Severity.HIGH), why + [f"{snow:.0f} cm snow"]
    elif snow >= 2:
        sev, why = max(sev, Severity.MODERATE), why + [f"{snow:.0f} cm snow"]
    gust = d.get("wind_gusts_10m_max") or 0
    if gust >= 80:
        sev, why = max(sev, Severity.HIGH), why + [f"gusts {gust:.0f} km/h"]
    elif gust >= 60:
        sev, why = max(sev, Severity.MODERATE), why + [f"gusts {gust:.0f} km/h"]
    elif gust >= 50:
        sev, why = max(sev, Severity.LOW), why + [f"gusts {gust:.0f} km/h"]
    rain = d.get("precipitation_sum") or 0
    if rain >= 25:
        sev, why = max(sev, Severity.MODERATE), why + [f"{rain:.0f} mm precip"]
    pop = d.get("precipitation_probability_max")
    if sev and pop is not None and pop < 30 and code not in (45, 48):
        sev = max(sev - 1, Severity.LOW)
        why.append(f"but only {pop}% precip probability")
    return int(sev), list(dict.fromkeys(why))


def lead_time_confidence(days_out: int) -> str:
    return "high" if days_out <= 1 else "medium" if days_out <= 6 else "low"


async def fetch(ctx: TripContext) -> SourceResult:
    t0 = time.perf_counter()
    if ctx.days_out > MAX_DAYS_OUT:
        return SourceResult(
            status=SourceStatus(
                key=KEY, name=NAME, status="not_applicable", horizon=HORIZON, url=PUBLIC_URL,
                message=f"Trip is {ctx.days_out} days out; beyond the 16-day forecast horizon.",
            )
        )
    airports = ctx.all_airports
    data = await http.get_json(
        URL,
        {
            "latitude": ",".join(str(a.lat) for a in airports),
            "longitude": ",".join(str(a.lon) for a in airports),
            "daily": DAILY,
            "timezone": "auto",
            "start_date": ctx.travel_date.isoformat(),
            "end_date": ctx.travel_date.isoformat(),
        },
        ttl=900,
    )
    data = data if isinstance(data, list) else [data]
    signals = []
    conf = lead_time_confidence(ctx.days_out)
    for a, loc in zip(airports, data):
        daily = loc.get("daily") or {}
        day = {k: (v[0] if isinstance(v, list) and v else None) for k, v in daily.items()}
        sev, why = assess_day(day)
        summary = (
            f"{WMO.get(day.get('weather_code'), 'clear/cloudy')}; precip {day.get('precipitation_sum')} mm "
            f"({day.get('precipitation_probability_max')}%), snow {day.get('snowfall_sum')} cm, "
            f"max gust {day.get('wind_gusts_10m_max')} km/h"
        )
        signals.append(
            Signal(
                source=KEY,
                source_name=NAME,
                location=a.iata,
                category="weather_model",
                severity=sev,
                title=("Model forecast: " + ", ".join(why)) if why else "Model forecast: no significant weather",
                detail=summary,
                confidence=conf,
                relevance=f"Daily model forecast for {ctx.travel_date} ({ctx.days_out} day(s) lead time).",
                url=f"{PUBLIC_URL}en/docs#latitude={a.lat}&longitude={a.lon}",
                raw=str(day),
            )
        )
    return SourceResult(
        status=SourceStatus(
            key=KEY, name=NAME, status="ok", horizon=HORIZON, url=PUBLIC_URL, records=len(signals),
            message=f"Daily forecast for {len(signals)} airport(s); lead-time confidence: {conf}.",
            latency_ms=int((time.perf_counter() - t0) * 1000),
        ),
        signals=signals,
    )
