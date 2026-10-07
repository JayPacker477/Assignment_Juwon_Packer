"""AI layer (Anthropic Claude).

1. parse_trip: free text ("NYC to SF tomorrow") -> structured trip.
2. write_briefing: structured evidence -> plain-English briefing with cited evidence IDs.

Guardrails: the model only sees normalized evidence, must cite evidence IDs, citations are
validated server-side, and the deterministic level stays authoritative. Every function has
a non-AI fallback so the app works (with a visible badge) when the API is unavailable.
"""

from __future__ import annotations

import json
import re
import time
from datetime import date, timedelta
from typing import Any, Optional

from . import config

try:
    from anthropic import AsyncAnthropic
except ImportError:  # pragma: no cover
    AsyncAnthropic = None  # type: ignore

_client = None


def enabled() -> bool:
    return bool(config.ANTHROPIC_API_KEY and AsyncAnthropic)


def _anthropic():
    global _client
    if _client is None:
        _client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY, timeout=45, max_retries=1)
    return _client


async def _tool_call(model: str, system: str, user: str, tool: dict, max_tokens: int) -> dict:
    resp = await _anthropic().messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
        tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
    )
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("model output truncated (max_tokens)")
    missing = [k for k in tool["input_schema"].get("required", []) if not any(
        b.type == "tool_use" and k in b.input for b in resp.content)]
    if missing:
        raise RuntimeError(f"model output missing required fields: {missing}")
    for block in resp.content:
        if block.type == "tool_use":
            return dict(block.input)
    raise RuntimeError("model returned no structured output")


# ---------------------------------------------------------------- trip parsing

PARSE_TOOL = {
    "name": "set_trip",
    "description": "Record the structured U.S. trip the user described.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "origin": {"type": "string", "description": "Origin city or airport as the user meant it, e.g. 'New York' or 'JFK'"},
            "destination": {"type": "string"},
            "origin_airport": {"type": "string", "description": "IATA code ONLY if the user named a specific airport"},
            "destination_airport": {"type": "string", "description": "IATA code ONLY if the user named a specific airport"},
            "date": {"type": "string", "description": "Travel date YYYY-MM-DD"},
            "assumptions": {"type": "array", "items": {"type": "string"}, "description": "Anything you had to assume"},
        },
        "required": ["origin", "destination", "date", "assumptions"],
    },
}

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _parse_date_phrase(text: str, today: date) -> tuple[Optional[date], str]:
    t = text.lower()
    rules: list[tuple[str, Any]] = [
        (r"\bday after tomorrow\b", lambda m: today + timedelta(days=2)),
        (r"\bin (\d{1,3}) days?\b", lambda m: today + timedelta(days=int(m[1]))),
        (r"\bnext week\b", lambda m: today + timedelta(days=7)),
        (r"\btomorrow\b", lambda m: today + timedelta(days=1)),
        (r"\b(today|tonight)\b", lambda m: today),
        (r"\b(\d{4})-(\d{2})-(\d{2})\b", lambda m: date(int(m[1]), int(m[2]), int(m[3]))),
        (r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", lambda m: _md(today, int(m[1]), int(m[2]), m[3])),
        (r"\b(" + "|".join(MONTHS) + r")[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b",
         lambda m: _md(today, MONTHS.index(m[1][:3]) + 1, int(m[2]), None)),
        (r"\b(next\s+)?(" + "|".join(WEEKDAYS) + r")\b",
         lambda m: today + timedelta(days=(WEEKDAYS.index(m[2]) - today.weekday()) % 7 or 7)),
    ]
    for pattern, fn in rules:
        m = re.search(pattern, t)
        if m:
            try:
                return fn(m), (text[: m.start()] + text[m.end():]).strip()
            except ValueError:
                continue
    return None, text


def _md(today: date, month: int, day: int, year: Optional[str]) -> date:
    if year:
        y = int(year) + (2000 if len(year) == 2 else 0)
        return date(y, month, day)
    d = date(today.year, month, day)
    return d if d >= today else date(today.year + 1, month, day)


def parse_trip_rules(text: str, today: date) -> dict:
    when, rest = _parse_date_phrase(text, today)
    rest = re.sub(r"\b(on|for|flying|fly|trip|from|leaving|departing)\b", " ", rest, flags=re.I)
    parts = re.split(r"\s+(?:to|->|→|–|—)\s+|\s*(?:->|→)\s*", rest.strip(" ,."), maxsplit=1, flags=re.I)
    if len(parts) != 2:
        raise ValueError("Could not find an origin and destination. Try 'Chicago to Denver tomorrow'.")
    assumptions = [] if when else ["No date found; assumed today."]
    return {
        "origin": parts[0].strip(" ,."),
        "destination": parts[1].strip(" ,."),
        "date": (when or today).isoformat(),
        "assumptions": assumptions,
    }


async def parse_trip(text: str, today: date) -> dict:
    if enabled():
        t0 = time.perf_counter()
        try:
            out = await _tool_call(
                config.ANTHROPIC_PARSE_MODEL,
                "You extract U.S. domestic trip details for a corporate travel-risk tool. "
                f"Today is {today:%A, %B %d, %Y}. Resolve relative dates against today. "
                "Keep city names as the user meant them (e.g. 'NYC' -> 'New York'); only fill an "
                "airport code when the user explicitly named an airport.",
                text,
                PARSE_TOOL,
                400,
            )
            date.fromisoformat(out["date"])
            out["parser"] = {"mode": "ai", "model": config.ANTHROPIC_PARSE_MODEL,
                             "latency_ms": int((time.perf_counter() - t0) * 1000)}
            return out
        except Exception as e:  # noqa: BLE001
            fallback = parse_trip_rules(text, today)
            fallback["parser"] = {"mode": "fallback", "error": str(e)[:200]}
            return fallback
    out = parse_trip_rules(text, today)
    out["parser"] = {"mode": "fallback", "error": "ANTHROPIC_API_KEY not configured"}
    return out


# ---------------------------------------------------------------- briefing

BRIEFING_TOOL = {
    "name": "write_briefing",
    "description": "Write the traveler-facing disruption briefing.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "headline": {"type": "string", "description": "<= 12 words, plain English"},
            "summary": {"type": "string", "description": "2-4 sentences. Cite evidence inline like [E1]."},
            "ai_risk_level": {"type": "string", "enum": ["LOW", "MODERATE", "HIGH", "UNKNOWN"]},
            "level_rationale": {"type": "string", "description": "Why you agree or disagree with the rule-based level"},
            "key_risks": {
                "type": "array",
                "description": "Every evidence item with severity >= 2 that counts toward the score must be covered by a key risk. May be empty only if there are none.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "title": {"type": "string"},
                        "explanation": {"type": "string"},
                        "likelihood": {"type": "string", "enum": ["low", "medium", "high"]},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["title", "explanation", "likelihood", "evidence_ids"],
                },
            },
            "actions": {
                "type": "array",
                "minItems": 1,
                "description": "At least one action. For low risk, a single 'normal check-in, re-check the day before' style action is fine.",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "action": {"type": "string"},
                        "why": {"type": "string"},
                        "when": {"type": "string", "enum": ["now", "day_before", "day_of", "if_conditions_change"]},
                        "evidence_ids": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["action", "why", "when", "evidence_ids"],
                },
            },
            "uncertainty": {"type": "string", "description": "What is uncertain and why, incl. data gaps/horizons"},
            "watch_for": {"type": "array", "items": {"type": "string"}, "description": "What would change this assessment"},
        },
        "required": ["headline", "summary", "ai_risk_level", "level_rationale", "key_risks", "actions", "uncertainty", "watch_for"],
    },
}

BRIEFING_SYSTEM = """You are a travel-operations analyst writing a disruption briefing for a corporate traveler on a U.S. domestic trip.

Rules:
- Use ONLY the evidence provided. Never invent delays, alerts, numbers, or sources.
- Every key risk and action must cite the evidence IDs (e.g. "E2") that support it.
- Evidence with counts_toward_score=false is context only; do not treat it as a risk (you may mention why it was discounted).
- Severity-0 evidence is evidence of ABSENCE of a problem — use it to reassure when appropriate.
- Respect time horizons: live FAA status is about *now*; do not claim it predicts a trip days away unless the cause is structural.
- Be proportional. If risk is low, say so plainly and keep actions minimal (e.g. normal check-in). Don't manufacture worry.
- Actions must be concrete and practical for a traveler (check airline app, look for travel waivers, choose earlier flights,
  alternate airport, protect connections, allow extra ground time). Recommend an alternate airport only if the evidence shows it is better.
- The rule-based level is authoritative for the UI. Set ai_risk_level to your own independent judgement; if it differs, explain why in level_rationale.
- If no forecast evidence covers the trip date, ai_risk_level must be UNKNOWN: absence of data is not low risk. You may still mention seasonal/climatological concerns as things to watch, clearly labelled as general knowledge rather than evidence.
- Cite evidence as separate brackets, e.g. [E1][E2], never [E1, E2].
- Communicate uncertainty honestly, including any sources that failed or were out of horizon.
- Be concise: at most 4 key risks and 4 actions, one or two short sentences each. Merge related items.
- Fill in the fields in schema order: headline, summary, ai_risk_level, level_rationale, key_risks, actions, uncertainty, watch_for."""


def _evidence_payload(trip: dict, risk: dict, signals: list[dict], statuses: list[dict]) -> str:
    slim = [
        {k: s.get(k) for k in ("id", "source_name", "location", "category", "severity", "title", "detail",
                               "confidence", "relevance", "counts_toward_score", "valid_from", "valid_to")}
        for s in signals
    ]
    return json.dumps(
        {
            "trip": trip,
            "rule_based_assessment": {k: risk[k] for k in ("level", "confidence", "confidence_reasons", "segments", "alternates")},
            "evidence": slim,
            "sources_checked": [{k: s[k] for k in ("name", "status", "message", "horizon")} for s in statuses],
        },
        default=str,
        indent=1,
    )


def _coerce_list(v: Any) -> list:
    """Models occasionally return nested arrays as JSON-encoded strings."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return [v] if v.strip() else []
    return v if isinstance(v, list) else []


def normalize_briefing(b: dict) -> dict:
    for field in ("key_risks", "actions"):
        items = []
        for it in _coerce_list(b.get(field)):
            if isinstance(it, str):
                try:
                    it = json.loads(it)
                except ValueError:
                    continue
            if isinstance(it, dict):
                it["evidence_ids"] = [str(i) for i in _coerce_list(it.get("evidence_ids"))]
                items.append(it)
        b[field] = items
    b["watch_for"] = [str(w) for w in _coerce_list(b.get("watch_for"))]
    for field in ("headline", "summary", "level_rationale", "uncertainty", "ai_risk_level"):
        b[field] = str(b.get(field) or "")
    return b


def validate_briefing(b: dict, valid_ids: set[str]) -> tuple[dict, list[str]]:
    """Drop citations to non-existent evidence; drop risks left with no support."""
    b = normalize_briefing(b)
    dropped: list[str] = []

    def clean(ids):
        good = [i for i in ids or [] if i in valid_ids]
        dropped.extend(i for i in ids or [] if i not in valid_ids)
        return good

    risks = []
    for r in b.get("key_risks", []):
        r["evidence_ids"] = clean(r.get("evidence_ids"))
        if r["evidence_ids"]:
            risks.append(r)
        else:
            dropped.append(f"risk:{r.get('title')}")
    b["key_risks"] = risks
    for a in b.get("actions", []):
        a["evidence_ids"] = clean(a.get("evidence_ids"))
    for field in ("summary", "level_rationale", "uncertainty"):
        b[field] = CITE_GROUP.sub(lambda m: _clean_group(m, valid_ids, dropped), b.get(field) or "")
    return b, dropped


CITE_GROUP = re.compile(r"\[\s*(E\d+(?:\s*[,;]\s*E\d+)*)\s*\]")


def _clean_group(m: re.Match, valid_ids: set[str], dropped: list[str]) -> str:
    """Normalize '[E1, E9]' to '[E1]' when E9 doesn't exist."""
    ids = re.findall(r"E\d+", m.group(1))
    dropped.extend(i for i in ids if i not in valid_ids)
    return "".join(f"[{i}]" for i in ids if i in valid_ids)


PLAYBOOK = {
    "ground_stop": ("Check flight status now and ask the airline about rebooking", "now"),
    "airport_closure": ("Contact the airline / travel desk to rebook", "now"),
    "ground_delay_program": ("Expect delays; check the airline app before leaving and avoid tight connections", "day_of"),
    "general_delay": ("Allow extra time and monitor flight status", "day_of"),
    "weather_alert": ("Check your airline for a travel waiver (free changes) and consider shifting the trip", "day_before"),
    "aviation_forecast": ("Prefer an earlier departure and avoid tight connections", "day_before"),
    "forecast": ("Re-check the forecast the day before departure", "day_before"),
    "weather_model": ("Re-check closer to departure; forecasts at this lead time can change", "if_conditions_change"),
    "enroute_hazard": ("Expect possible reroutes or turbulence; minor schedule impact likely", "day_of"),
}


def fallback_briefing(trip: dict, risk: dict, signals: list[dict]) -> dict:
    scored = [s for s in signals if s["counts_toward_score"] and s["severity"] >= 2]
    level = risk["level"]
    route = f"{trip['origin_airport']} → {trip['destination_airport']}"
    if level == "UNKNOWN":
        headline = f"Too far out to assess {route}"
    elif not scored:
        headline = f"No significant disruption signals for {route}"
    else:
        headline = f"{level.title()} disruption risk for {route}"
    risks = [
        {"title": s["title"], "explanation": s["relevance"], "evidence_ids": [s["id"]],
         "likelihood": "high" if s["confidence"] == "high" else "medium" if s["confidence"] == "medium" else "low"}
        for s in scored[:4]
    ]
    actions, seen = [], set()
    for s in scored:
        if s["category"] in PLAYBOOK and s["category"] not in seen:
            seen.add(s["category"])
            text, when = PLAYBOOK[s["category"]]
            actions.append({"action": text, "why": s["title"], "when": when, "evidence_ids": [s["id"]]})
    if not actions:
        actions.append({"action": "No action needed beyond normal check-in; re-run this check the day before.",
                        "why": "No moderate or high signals found.", "when": "day_before", "evidence_ids": []})
    return {
        "headline": headline,
        "summary": (
            f"Rule-based assessment: {level} risk with {risk['confidence']} confidence. "
            + (f"{len(scored)} signal(s) at moderate or higher severity." if scored else "No moderate or high severity signals.")
        ),
        "ai_risk_level": level,
        "level_rationale": "AI unavailable; rule-based level shown.",
        "key_risks": risks,
        "actions": actions,
        "uncertainty": " ".join(risk["confidence_reasons"]),
        "watch_for": ["New NWS alerts for either airport", "FAA ground stops / delay programs on the day of travel"],
    }


def _gaps(b: dict, signals: list[dict]) -> list[str]:
    gaps = []
    if not b["actions"]:
        gaps.append("actions")
    if not b["key_risks"] and any(s["counts_toward_score"] and s["severity"] >= 2 for s in signals):
        gaps.append("key_risks")
    return gaps


async def write_briefing(trip: dict, risk: dict, signals: list[dict], statuses: list[dict]) -> dict:
    meta: dict[str, Any] = {"mode": "fallback"}
    if enabled():
        t0 = time.perf_counter()
        try:
            payload = _evidence_payload(trip, risk, signals, statuses)
            valid = {s["id"] for s in signals}
            attempts = 0
            while True:
                attempts += 1
                out = await _tool_call(config.ANTHROPIC_MODEL, BRIEFING_SYSTEM, payload, BRIEFING_TOOL, 4000)
                out, dropped = validate_briefing(out, valid)
                gaps = _gaps(out, signals)
                if not gaps or attempts >= 2:
                    break
            # Never show e.g. a HIGH risk with no actions: backfill from the rule playbook and say so.
            backfilled = []
            if gaps:
                fb = fallback_briefing(trip, risk, signals)
                for g in gaps:
                    out[g] = fb[g]
                    backfilled.append(g)
            meta = {"mode": "ai", "model": config.ANTHROPIC_MODEL, "dropped_citations": dropped,
                    "latency_ms": int((time.perf_counter() - t0) * 1000), "attempts": attempts,
                    "backfilled": backfilled,
                    "disagrees_with_rules": out.get("ai_risk_level") != risk["level"]}
            return {**out, "meta": meta}
        except Exception as e:  # noqa: BLE001
            meta["error"] = f"{type(e).__name__}: {str(e)[:200]}"
    else:
        meta["error"] = "ANTHROPIC_API_KEY not configured"
    return {**fallback_briefing(trip, risk, signals), "meta": meta}
