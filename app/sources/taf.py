"""Aviation Weather Center TAFs: airport-specific aviation forecasts (~24–30h horizon).

TAFs are what airline dispatchers plan against, so they are the strongest weather
signal for near-term trips — ceilings/visibility and gusts drive ATC flow programs.
"""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Optional

from .. import http
from ..models import Severity, Signal, SourceResult, SourceStatus, TripContext
from ..timeutil import fmt_utc, from_epoch, overlaps, parse_iso

KEY = "taf"
NAME = "AviationWeather.gov TAF"
URL = "https://aviationweather.gov/api/data/taf"
PUBLIC_URL = "https://aviationweather.gov/data/taf/"
HORIZON = "~24–30 hours"


def parse_visibility(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace("+", "").replace("SM", "").replace("P", "").strip()
    try:
        total = 0.0
        for part in s.split():
            if "/" in part:
                n, d = part.split("/")
                total += float(n) / float(d)
            else:
                total += float(part)
        return total
    except ValueError:
        return None


def ceiling_ft(period: dict) -> Optional[int]:
    bases = [c.get("base") for c in period.get("clouds") or [] if c.get("cover") in ("BKN", "OVC", "OVX")]
    if period.get("vertVis"):
        bases.append(period["vertVis"])
    bases = [b for b in bases if isinstance(b, (int, float))]
    return int(min(bases)) if bases else None


def assess_period(p: dict) -> tuple[int, list[str]]:
    sev, why = Severity.NONE, []
    wx = (p.get("wxString") or "").upper()
    if any(t in wx for t in ("FZRA", "FZDZ", "PL")):
        sev, why = max(sev, Severity.HIGH), why + [f"freezing precip ({wx})"]
    if "+SN" in wx or "BLSN" in wx:
        sev, why = max(sev, Severity.HIGH), why + [f"heavy/blowing snow ({wx})"]
    elif "SN" in wx:
        sev, why = max(sev, Severity.MODERATE), why + [f"snow ({wx})"]
    if "TS" in wx:
        s = Severity.HIGH if "+TS" in wx else Severity.MODERATE
        sev, why = max(sev, s), why + [f"thunderstorms ({wx})"]
    if "FG" in wx and "BCFG" not in wx and "MIFG" not in wx:
        sev, why = max(sev, Severity.LOW), why + [f"fog ({wx})"]

    gust, wspd = p.get("wgst") or 0, p.get("wspd") or 0
    if gust >= 45 or wspd >= 35:
        sev, why = max(sev, Severity.HIGH), why + [f"wind {wspd}G{gust}kt"]
    elif gust >= 35 or wspd >= 25:
        sev, why = max(sev, Severity.MODERATE), why + [f"wind {wspd}G{gust}kt"]
    elif gust >= 28:
        sev, why = max(sev, Severity.LOW), why + [f"gusty wind {wspd}G{gust}kt"]

    vis, ceil = parse_visibility(p.get("visib")), ceiling_ft(p)
    if (ceil is not None and ceil < 500) or (vis is not None and vis < 1):
        sev, why = max(sev, Severity.MODERATE), why + [f"LIFR (ceiling {ceil or '—'}ft, vis {vis}SM)"]
    elif (ceil is not None and ceil < 1000) or (vis is not None and vis < 3):
        sev, why = max(sev, Severity.LOW), why + [f"IFR (ceiling {ceil or '—'}ft, vis {vis}SM)"]

    # TEMPO/PROB groups describe possible, not prevailing, conditions.
    change = (p.get("fcstChange") or "").upper()
    if sev and (change in ("TEMPO", "PROB") or p.get("probability")):
        sev = max(sev - 1, Severity.LOW)
        why = [f"{w} [{change or 'PROB'}{p.get('probability') or ''}]" for w in why]
    return int(sev), why


async def fetch(ctx: TripContext) -> SourceResult:
    t0 = time.perf_counter()
    if ctx.days_out > 1:
        return SourceResult(
            status=SourceStatus(
                key=KEY, name=NAME, status="not_applicable", horizon=HORIZON, url=PUBLIC_URL,
                message=f"Trip is {ctx.days_out} days out; TAFs only cover the next ~30 hours.",
            )
        )
    by_icao = {a.icao: a for a in ctx.all_airports}
    data = await http.get_json(URL, {"ids": ",".join(by_icao), "format": "json"}, ttl=600)
    latest = {}
    for t in data:
        if t.get("icaoId") in by_icao and (t["icaoId"] not in latest or t.get("mostRecent")):
            latest[t["icaoId"]] = t

    signals, partial = [], []
    for icao, taf in latest.items():
        a = by_icao[icao]
        valid_to = from_epoch(taf.get("validTimeTo"))
        periods = [
            p for p in taf.get("fcsts", [])
            if overlaps(from_epoch(p.get("timeFrom")), from_epoch(p.get("timeTo")), ctx.window_start, ctx.window_end)
        ]
        if not periods:
            continue
        sev, reasons = 0, []
        for p in periods:
            s, why = assess_period(p)
            sev = max(sev, s)
            reasons += [f"{fmt_utc(from_epoch(p.get('timeFrom')))}: {w}" for w in why]
        covered = valid_to and valid_to >= ctx.window_end - timedelta(hours=1)
        if not covered:
            partial.append(a.iata)
        signals.append(
            Signal(
                source=KEY,
                source_name=NAME,
                location=a.iata,
                category="aviation_forecast",
                severity=sev,
                title=(
                    "TAF: " + "; ".join(dict.fromkeys(r.split(": ", 1)[1] for r in reasons))
                    if reasons else "TAF: VFR, no significant weather forecast"
                ),
                detail="\n".join(reasons) or "All forecast periods overlapping the trip are benign.",
                confidence="high" if ctx.days_out == 0 and covered else "medium",
                relevance=(
                    f"Official aviation forecast for {icao}"
                    + ("" if covered else f"; only covers the trip date until {fmt_utc(valid_to)}")
                    + "."
                ),
                valid_from=from_epoch(taf.get("validTimeFrom")),
                valid_to=valid_to,
                issued_at=parse_iso(taf.get("issueTime")) if isinstance(taf.get("issueTime"), str) else from_epoch(taf.get("issueTime")),
                url=f"{PUBLIC_URL}?ids={icao}",
                raw=taf.get("rawTAF"),
            )
        )
    msg = f"TAFs for {len(latest)}/{len(by_icao)} airports; {len(signals)} overlap the trip window."
    if partial:
        msg += f" Partial coverage for {', '.join(partial)} (TAF ends before the trip day does)."
    return SourceResult(
        status=SourceStatus(
            key=KEY, name=NAME, status="ok", message=msg, horizon=HORIZON, url=PUBLIC_URL,
            records=len(signals), latency_ms=int((time.perf_counter() - t0) * 1000),
        ),
        signals=signals,
    )
