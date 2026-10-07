"""FAA National Airspace System status: live ground stops, delay programs, closures.

This is a *current-conditions* snapshot. It is highly predictive for same-day travel
and mostly irrelevant for later dates, except for structural causes (construction,
scheduled closures) which tend to persist.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional

from .. import http
from ..models import Severity, Signal, SourceResult, SourceStatus, TripContext
from ..timeutil import overlaps

KEY = "faa"
NAME = "FAA NAS Status"
URL = "https://nasstatus.faa.gov/api/airport-status-information"
PUBLIC_URL = "https://nasstatus.faa.gov/"
HORIZON = "Right now only (live snapshot)"

# NOTAM closures that don't affect scheduled airline passengers.
NON_AIRLINE_CLOSURE = re.compile(r"NON\s*SKED|TRANSIENT|\bGA\b|\bPPR\b|WINGSPAN|TAIL HGT", re.I)
PERSISTENT_CAUSE = re.compile(r"construction|runway|closure|equipment|outage", re.I)
NOTAM_WINDOW = re.compile(r"(\d{10})-(\d{10})")


def parse_minutes(text: Optional[str]) -> int:
    if not text:
        return 0
    hours = re.search(r"(\d+)\s*hour", text)
    mins = re.search(r"(\d+)\s*minute", text)
    return (int(hours.group(1)) * 60 if hours else 0) + (int(mins.group(1)) if mins else 0)


def _notam_window(reason: str) -> tuple[Optional[datetime], Optional[datetime]]:
    m = NOTAM_WINDOW.search(reason)
    if not m:
        return None, None
    fmt = "%y%m%d%H%M"
    try:
        return (
            datetime.strptime(m.group(1), fmt).replace(tzinfo=timezone.utc),
            datetime.strptime(m.group(2), fmt).replace(tzinfo=timezone.utc),
        )
    except ValueError:
        return None, None


def parse_status_xml(xml_text: str) -> tuple[Optional[datetime], list[dict]]:
    """Flatten the FAA XML into records: {type, arpt, reason, ...fields}."""
    root = ET.fromstring(xml_text)
    updated = None
    ut = root.findtext("Update_Time")
    if ut:
        try:
            updated = datetime.strptime(ut.strip(), "%a %b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    records = []
    for dtype in root.findall("Delay_type"):
        type_name = (dtype.findtext("Name") or "").strip()
        for el in dtype.iter():
            arpt = el.findtext("ARPT")
            if not arpt or el.find("ARPT") is None:
                continue
            rec = {"type": type_name, "arpt": arpt.strip().upper()}
            for child in el:
                if child.tag == "Arrival_Departure":
                    kind = child.get("Type", "")
                    rec.setdefault("directions", []).append(
                        {
                            "type": kind,
                            "min": child.findtext("Min"),
                            "max": child.findtext("Max"),
                            "trend": child.findtext("Trend"),
                        }
                    )
                elif len(child) == 0 and child.text:
                    rec[child.tag.lower()] = child.text.strip()
            records.append(rec)
    return updated, records


def classify(rec: dict) -> tuple[int, str, str]:
    """Returns (severity, category, title) for the record as of *right now*."""
    t = rec["type"].lower()
    reason = rec.get("reason", "")
    if "ground stop" in t:
        return Severity.HIGH, "ground_stop", f"Ground stop in effect ({reason or 'reason not given'})"
    if "ground delay" in t:
        avg = parse_minutes(rec.get("avg"))
        sev = Severity.HIGH if avg >= 60 else Severity.MODERATE if avg >= 30 else Severity.LOW
        return sev, "ground_delay_program", f"Ground delay program: avg {rec.get('avg', '?')} ({reason})"
    if "closure" in t:
        if NON_AIRLINE_CLOSURE.search(reason):
            return Severity.NONE, "closure_non_airline", "Restriction for non-scheduled / GA / oversized aircraft only"
        return Severity.HIGH, "airport_closure", "Airport closure"
    if "arrival/departure" in t:
        dirs = rec.get("directions", [])
        worst = max((parse_minutes(d.get("max")) for d in dirs), default=0)
        sev = Severity.MODERATE if worst >= 45 else Severity.LOW
        kinds = "/".join(sorted({d["type"].lower() for d in dirs})) or "arrival/departure"
        return sev, "general_delay", f"{kinds.capitalize()} delays up to {worst} min ({reason})"
    return Severity.LOW, "other", f"{rec['type']}: {reason}"


def _detail(rec: dict) -> str:
    parts = [f"{k}={v}" for k, v in rec.items() if k not in ("type", "arpt", "directions")]
    for d in rec.get("directions", []):
        parts.append(f"{d['type']} {d['min']}–{d['max']} trend {d['trend']}")
    return f"{rec['type']} @ {rec['arpt']}: " + "; ".join(parts)


def to_signal(rec: dict, ctx: TripContext, updated: Optional[datetime]) -> Signal:
    sev, category, title = classify(rec)
    reason = rec.get("reason", "")
    counts, confidence = True, "high"
    valid_from, valid_to = _notam_window(reason) if "closure" in category else (None, None)

    if category == "closure_non_airline":
        counts = False
        relevance = "Applies only to non-scheduled/general-aviation or oversized aircraft; airline passengers unaffected."
    elif valid_to and not overlaps(valid_from, valid_to, ctx.window_start, ctx.window_end):
        sev, counts = Severity.NONE, False
        relevance = "Closure window does not overlap the trip date."
    elif ctx.days_out == 0:
        relevance = "Live FAA status for today's travel."
    elif PERSISTENT_CAUSE.search(reason) or valid_to:
        confidence = "medium" if ctx.days_out == 1 else "low"
        if ctx.days_out >= 2:
            sev = max(sev - 1, Severity.LOW)
        relevance = (
            f"Live snapshot, but the cause ('{reason}') is structural and may persist to the trip date "
            f"({ctx.days_out} day(s) out)."
        )
    else:
        counts, confidence = False, "low"
        relevance = (
            f"Live snapshot only; weather/volume-driven programs rarely carry over {ctx.days_out} day(s). "
            "Shown for context, not scored."
        )

    return Signal(
        source=KEY,
        source_name=NAME,
        location=rec["arpt"],
        category=category,
        severity=int(sev),
        title=title,
        detail=_detail(rec),
        confidence=confidence,
        relevance=relevance,
        counts_toward_score=counts,
        valid_from=valid_from,
        valid_to=valid_to,
        issued_at=updated,
        url=PUBLIC_URL,
        raw=reason or None,
    )


async def fetch(ctx: TripContext) -> SourceResult:
    t0 = time.perf_counter()
    xml_text = await http.get_text(URL, ttl=120)
    updated, records = parse_status_xml(xml_text)
    codes = {a.iata for a in ctx.all_airports}
    mine = [r for r in records if r["arpt"] in codes]
    signals = [to_signal(r, ctx, updated) for r in mine]
    msg = (
        f"{len(records)} active NAS events nationally; {len(mine)} at your airports"
        + (f" (updated {updated:%H:%MZ})" if updated else "")
    )
    if ctx.days_out > 0:
        msg += ". Live data — only structural causes are carried forward to future dates."
    return SourceResult(
        status=SourceStatus(
            key=KEY, name=NAME, status="ok", message=msg, horizon=HORIZON, url=PUBLIC_URL,
            records=len(mine), latency_ms=int((time.perf_counter() - t0) * 1000),
        ),
        signals=signals,
    )
