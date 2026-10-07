from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from app import airports
from app.ai import fallback_briefing, parse_trip_rules, validate_briefing
from app.geo import great_circle_points, path_intersects_polygon, point_in_polygon
from app.models import Severity, Signal, SourceStatus
from app.risk import assign_ids, score
from app.service import build_context
from app.sources import faa, nws, open_meteo, taf

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 10, 5, 18, 0, tzinfo=timezone.utc)


def ctx_for(o="JFK", d="SFO", days_out=0):
    return build_context(
        [airports.by_iata(o)], [airports.by_iata(d)], NOW.date() + timedelta(days=days_out), NOW
    )


# ---------------------------------------------------------------- airports

def test_metro_resolution_puts_primary_first():
    codes = [a.iata for a in airports.resolve_local("NYC")]
    assert codes[0] == "JFK" and {"LGA", "EWR"} <= set(codes)


def test_all_metro_codes_exist_in_dataset():
    missing = [c for codes in airports.METROS.values() for c in codes if not airports.by_iata(c)]
    assert missing == []


@pytest.mark.parametrize("q,expected", [("sfo", "SFO"), ("KORD", "ORD"), ("Denver", "DEN"), ("Portland, ME", "PWM")])
def test_resolve_codes_and_cities(q, expected):
    assert airports.resolve_local(q)[0].iata == expected


def test_city_match_prefers_large_airports():
    assert [a.iata for a in airports.resolve_local("Seattle")] == ["SEA"]


# ---------------------------------------------------------------- geo

def test_great_circle_endpoints():
    pts = great_circle_points(40.64, -73.78, 37.62, -122.37, n=10)
    assert pts[0] == pytest.approx((40.64, -73.78), abs=1e-6)
    assert pts[-1] == pytest.approx((37.62, -122.37), abs=1e-6)


def test_polygon_intersection():
    square = [(30, -100), (45, -100), (45, -90), (30, -90)]
    assert point_in_polygon(40, -95, square)
    assert not point_in_polygon(40, -80, square)
    assert path_intersects_polygon(great_circle_points(40.64, -73.78, 37.62, -122.37), square)


# ---------------------------------------------------------------- FAA

def test_faa_parses_real_snapshot():
    updated, records = faa.parse_status_xml((FIXTURES / "faa_status.xml").read_text())
    assert updated is not None and records
    assert all(r["arpt"] and r["type"] for r in records)


def test_faa_non_airline_closure_is_not_scored():
    rec = {"type": "Airport Closures", "arpt": "LAX",
           "reason": "!LAX 05/277 LAX AD AP CLSD TO NON SKED TRANSIENT GA ACFT EXC 24HR PPR 2605271826-2705281600"}
    sig = faa.to_signal(rec, ctx_for("LAX", "SFO"), NOW)
    assert sig.severity == 0 and not sig.counts_toward_score


def test_faa_ground_stop_and_gdp_severity():
    xml = """<AIRPORT_STATUS_INFORMATION><Update_Time>Mon Oct 5 21:41:13 2026 GMT</Update_Time>
    <Delay_type><Name>Ground Stops</Name><Ground_Stop_List><Program><ARPT>SFO</ARPT>
    <Reason>low ceilings</Reason><End_Time>9:45 pm EDT</End_Time></Program></Ground_Stop_List></Delay_type>
    <Delay_type><Name>Ground Delay Programs</Name><Ground_Delay_List><Ground_Delay><ARPT>JFK</ARPT>
    <Reason>volume</Reason><Avg>1 hour and 5 minutes</Avg><Max>3 hours</Max></Ground_Delay></Ground_Delay_List></Delay_type>
    </AIRPORT_STATUS_INFORMATION>"""
    _, recs = faa.parse_status_xml(xml)
    sev = {r["arpt"]: faa.classify(r)[0] for r in recs}
    assert sev == {"SFO": Severity.HIGH, "JFK": Severity.HIGH}


def test_faa_weather_program_not_carried_to_future_dates():
    rec = {"type": "Ground Delay Programs", "arpt": "SFO", "reason": "low ceilings", "avg": "1 hour"}
    today = faa.to_signal(rec, ctx_for(days_out=0), NOW)
    later = faa.to_signal(rec, ctx_for(days_out=2), NOW)
    assert today.counts_toward_score and today.severity == 3
    assert not later.counts_toward_score


def test_faa_construction_program_persists_with_lower_confidence():
    rec = {"type": "Ground Delay Programs", "arpt": "JFK", "reason": "runway construction", "avg": "2 hours"}
    sig = faa.to_signal(rec, ctx_for(days_out=1), NOW)
    assert sig.counts_toward_score and sig.confidence == "medium"


def test_parse_minutes():
    assert faa.parse_minutes("2 hours and 59 minutes") == 179
    assert faa.parse_minutes("44 minutes") == 44


# ---------------------------------------------------------------- weather rules

@pytest.mark.parametrize("event,sev", [
    ("Winter Storm Warning", 3), ("Dense Fog Advisory", 2), ("Rip Current Statement", 0), ("Heat Advisory", 1),
])
def test_nws_alert_severity(event, sev):
    assert nws.alert_severity(event) == sev


def test_nws_forecast_text():
    assert nws.forecast_severity("Snow likely. Heavy snow at times.", 80)[0] == 3
    assert nws.forecast_severity("A slight chance of thunderstorms.", 20)[0] == 1
    assert nws.forecast_severity("Sunny, with gusts as high as 52 mph.", 0)[0] == 2
    assert nws.forecast_severity("Sunny.", 0) == (0, [])


def test_taf_period_rules():
    assert taf.assess_period({"wxString": "FZRA", "wspd": 10})[0] == 3
    assert taf.assess_period({"wxString": "TSRA", "wspd": 10})[0] == 2
    assert taf.assess_period({"wxString": "TSRA", "fcstChange": "TEMPO"})[0] == 1
    assert taf.assess_period({"visib": "1/2", "clouds": [{"cover": "OVC", "base": 200}]})[0] == 2
    assert taf.assess_period({"visib": "6+", "wspd": 8, "clouds": [{"cover": "FEW", "base": 25000}]})[0] == 0


def test_taf_visibility_parsing():
    assert taf.parse_visibility("6+") == 6
    assert taf.parse_visibility("1 1/2") == 1.5
    assert taf.parse_visibility(3) == 3


def test_open_meteo_day_rules():
    assert open_meteo.assess_day({"weather_code": 75, "snowfall_sum": 15})[0] == 3
    assert open_meteo.assess_day({"weather_code": 95, "precipitation_probability_max": 70})[0] == 2
    assert open_meteo.assess_day({"weather_code": 95, "precipitation_probability_max": 10})[0] == 1
    assert open_meteo.assess_day({"weather_code": 1, "wind_gusts_10m_max": 20})[0] == 0


# ---------------------------------------------------------------- scoring

def sig(source, loc, sev, category="weather_model", counts=True):
    return Signal(source=source, source_name=source, location=loc, category=category, severity=sev,
                  title=f"{source} {sev}", detail="", confidence="high", relevance="", counts_toward_score=counts)


def ok(key):
    return SourceStatus(key=key, name=key, status="ok", message="", horizon="", url="")


def test_score_takes_max_and_ignores_context_signals():
    ctx = ctx_for()
    signals = assign_ids([sig("open_meteo", "JFK", 1), sig("faa", "SFO", 3, "ground_delay_program", counts=False),
                          sig("taf", "SFO", 2, "aviation_forecast")])
    r = score(ctx, signals, [ok("open_meteo"), ok("taf")])
    assert r["level"] == "MODERATE"
    assert [s.id for s in signals if s.source == "taf"] == r["drivers"]


def test_score_unknown_without_any_forecast():
    ctx = ctx_for(days_out=30)
    r = score(ctx, [], [ok("nws")])
    assert r["level"] == "UNKNOWN" and r["confidence"] == "very low"


def test_confidence_drops_when_a_source_fails():
    ctx = ctx_for(days_out=0)
    failed = SourceStatus(key="taf", name="TAF", status="error", message="", horizon="", url="")
    r = score(ctx, [sig("open_meteo", "JFK", 0)], [ok("open_meteo"), failed])
    assert r["confidence"] == "medium"


def test_corroboration_flag():
    ctx = ctx_for()
    signals = assign_ids([sig("taf", "JFK", 2, "aviation_forecast"), sig("nws", "JFK", 2, "forecast")])
    seg = score(ctx, signals, [ok("taf")])["segments"][0]
    assert seg["corroborated"]


# ---------------------------------------------------------------- AI layer (non-network parts)

TODAY = date(2026, 10, 5)  # Monday


@pytest.mark.parametrize("text,o,d,when", [
    ("New York to San Francisco tomorrow", "New York", "San Francisco", date(2026, 10, 6)),
    ("from Boston -> Las Vegas today", "Boston", "Las Vegas", TODAY),
    ("Chicago to Denver next Friday", "Chicago", "Denver", date(2026, 10, 9)),
    ("Seattle to Miami in 30 days", "Seattle", "Miami", date(2026, 11, 4)),
    ("ATL to ORD on 2026-12-01", "ATL", "ORD", date(2026, 12, 1)),
    ("JFK → LAX Oct 20", "JFK", "LAX", date(2026, 10, 20)),
])
def test_rule_parser(text, o, d, when):
    p = parse_trip_rules(text, TODAY)
    assert (p["origin"], p["destination"], p["date"]) == (o, d, when.isoformat())


def test_rule_parser_rejects_garbage():
    with pytest.raises(ValueError):
        parse_trip_rules("what's the weather", TODAY)


def test_validate_briefing_drops_hallucinated_citations():
    b = {
        "summary": "Storms expected [E1] and a ground stop [E9].",
        "level_rationale": "", "uncertainty": "",
        "key_risks": [
            {"title": "Storms", "explanation": "", "likelihood": "high", "evidence_ids": ["E1"]},
            {"title": "Invented", "explanation": "", "likelihood": "high", "evidence_ids": ["E9"]},
        ],
        "actions": [{"action": "x", "why": "", "when": "now", "evidence_ids": ["E1", "E7"]}],
    }
    out, dropped = validate_briefing(b, {"E1", "E2"})
    assert [r["title"] for r in out["key_risks"]] == ["Storms"]
    assert out["actions"][0]["evidence_ids"] == ["E1"]
    assert "[E9]" not in out["summary"] and "E9" in dropped and "E7" in dropped


def test_validate_briefing_normalizes_grouped_citations():
    b = {"summary": "Clear at both ends [E1, E2, E9].", "level_rationale": "", "uncertainty": "", "key_risks": [], "actions": []}
    out, dropped = validate_briefing(b, {"E1", "E2"})
    assert out["summary"] == "Clear at both ends [E1][E2]." and dropped == ["E9"]


def test_fallback_briefing_low_risk_has_minimal_action():
    trip = {"origin_airport": "JFK", "destination_airport": "SFO"}
    risk = {"level": "LOW", "confidence": "high", "confidence_reasons": ["x"]}
    b = fallback_briefing(trip, risk, [])
    assert b["key_risks"] == [] and len(b["actions"]) == 1


def test_write_briefing_validates_model_output(monkeypatch):
    import asyncio

    from app import ai, config

    async def fake_tool_call(model, system, user, tool, max_tokens):
        assert '"id": "E1"' in user  # model only sees normalized evidence
        return {
            "headline": "h", "summary": "See [E1] and [E5].", "ai_risk_level": "HIGH", "level_rationale": "r",
            "key_risks": [{"title": "t", "explanation": "", "likelihood": "high", "evidence_ids": ["E1"]}],
            "actions": [], "uncertainty": "", "watch_for": [],
        }

    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(ai, "_tool_call", fake_tool_call)
    signals = [s.model_dump(mode="json") for s in assign_ids([sig("taf", "JFK", 2, "aviation_forecast")])]
    risk = {"level": "MODERATE", "confidence": "high", "confidence_reasons": [], "segments": [], "alternates": []}
    out = asyncio.run(ai.write_briefing({"origin_airport": "JFK", "destination_airport": "SFO"}, risk, signals, []))
    assert out["meta"]["mode"] == "ai" and out["meta"]["disagrees_with_rules"]
    assert out["meta"]["dropped_citations"] == ["E5"] and "[E5]" not in out["summary"]
    # Model returned no actions twice -> one retry, then backfilled from the rule playbook.
    assert out["meta"]["attempts"] == 2 and out["meta"]["backfilled"] == ["actions"] and out["actions"]


def test_validate_briefing_handles_stringified_arrays():
    import json as _json
    b = {
        "summary": "x", "level_rationale": "", "uncertainty": "", "headline": "h", "ai_risk_level": "LOW",
        "key_risks": _json.dumps([{"title": "t", "explanation": "", "likelihood": "low", "evidence_ids": ["E1"]}]),
        "actions": [_json.dumps({"action": "a", "why": "", "when": "now", "evidence_ids": "[\"E1\"]"}), "garbage"],
        "watch_for": "[\"w\"]",
    }
    out, _ = validate_briefing(b, {"E1"})
    assert out["key_risks"][0]["evidence_ids"] == ["E1"]
    assert out["actions"][0]["evidence_ids"] == ["E1"] and len(out["actions"]) == 1
    assert out["watch_for"] == ["w"]
