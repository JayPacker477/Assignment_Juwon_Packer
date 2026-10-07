"""Deterministic risk scoring.

The rule engine — not the LLM — owns the risk level, so the headline number is
reproducible and auditable. The AI layer explains it and may flag disagreement.
"""

from __future__ import annotations

from .models import SEVERITY_LABEL, Signal, SourceStatus, TripContext

LEVEL = {0: "LOW", 1: "LOW", 2: "MODERATE", 3: "HIGH"}
SOURCE_ORDER = {"faa": 0, "nws": 1, "taf": 2, "sigmet": 3, "open_meteo": 4}
CONF_ORDER = ["very low", "low", "medium", "high"]
FORECAST_CATEGORIES = {"forecast", "aviation_forecast", "weather_model"}


def assign_ids(signals: list[Signal]) -> list[Signal]:
    ordered = sorted(
        signals,
        key=lambda s: (not s.counts_toward_score, -s.severity, SOURCE_ORDER.get(s.source, 9), s.location),
    )
    for i, s in enumerate(ordered, 1):
        s.id = f"E{i}"
    return ordered


def _at(s: Signal, code: str) -> bool:
    return code in s.location.split(",")


def location_summary(signals: list[Signal], code: str) -> dict:
    mine = [s for s in signals if _at(s, code)]
    scored = [s for s in mine if s.counts_toward_score]
    sev = max((s.severity for s in scored), default=0)
    strong_sources = {s.source for s in scored if s.severity >= 2}
    return {
        "severity": sev,
        "level": LEVEL[sev],
        "signal_ids": [s.id for s in mine],
        "corroborated": len(strong_sources) >= 2,
        "top": next((s.title for s in scored if s.severity == sev and sev > 0), None),
    }


def confidence(ctx: TripContext, statuses: list[SourceStatus]) -> tuple[str, list[str]]:
    d = ctx.days_out
    if d == 0:
        idx, why = 3, ["Same-day trip: live FAA status, official TAFs and active SIGMETs all apply."]
    elif d == 1:
        idx, why = 3, ["Trip is tomorrow: official TAFs cover at least part of the day; live FAA status is only partly predictive."]
    elif d <= 4:
        idx, why = 2, [f"Trip is {d} days out: relying on forecasts, which can still shift."]
    elif d <= 15:
        idx, why = 1, [f"Trip is {d} days out: only model forecasts are available and skill drops with lead time."]
    else:
        idx, why = 0, [f"Trip is {d} days out: beyond all forecast horizons; no trip-specific weather data exists yet."]
    failed = [s.name for s in statuses if s.status == "error"]
    if failed:
        idx = max(idx - 1, 0)
        why.append(f"Source(s) unavailable: {', '.join(failed)}. Absence of evidence is weaker.")
    return CONF_ORDER[idx], why


def score(ctx: TripContext, signals: list[Signal], statuses: list[SourceStatus]) -> dict:
    o, d = ctx.origin[0], ctx.destination[0]
    enroute = [s for s in signals if s.location == "EN-ROUTE"]
    enroute_scored = [s for s in enroute if s.counts_toward_score]
    en_sev = max((s.severity for s in enroute_scored), default=0)

    segments = [
        {"key": "origin", "label": f"Departure · {o.iata}", "airport": o.iata, **location_summary(signals, o.iata)},
        {
            "key": "enroute", "label": "En-route", "airport": None, "severity": en_sev, "level": LEVEL[en_sev],
            "signal_ids": [s.id for s in enroute], "corroborated": False,
            "top": next((s.title for s in enroute_scored if s.severity == en_sev and en_sev > 0), None),
        },
        {"key": "destination", "label": f"Arrival · {d.iata}", "airport": d.iata, **location_summary(signals, d.iata)},
    ]
    overall = max(seg["severity"] for seg in segments)
    conf, conf_why = confidence(ctx, statuses)

    # No forecast covering the trip date means "we don't know", not "low risk".
    has_forecast = any(s.category in FORECAST_CATEGORIES for s in signals)
    level = LEVEL[overall]
    if not has_forecast and overall < 2:
        level = "UNKNOWN"

    primary = {o.iata, d.iata}
    drivers = [
        s.id for s in signals
        if s.counts_toward_score and s.severity == overall and overall > 0
        and (s.location == "EN-ROUTE" or primary & set(s.location.split(",")))
    ]
    alternates = []
    for side, group in (("origin", ctx.origin[1:]), ("destination", ctx.destination[1:])):
        for a in group:
            summary = location_summary(signals, a.iata)
            alternates.append({"side": side, "iata": a.iata, "name": a.name, **summary})

    return {
        "level": level,
        "severity": overall,
        "severity_label": SEVERITY_LABEL[overall],
        "confidence": conf,
        "confidence_reasons": conf_why,
        "segments": segments,
        "drivers": drivers,
        "alternates": alternates,
    }
