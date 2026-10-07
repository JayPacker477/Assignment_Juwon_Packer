"""Live-demo pre-flight check.

1. Verifies everything the demo depends on: the Anthropic key and models, all five live data
   sources, and (optionally) that the app server is up with AI enabled.
2. Scans ~30 major airports using the app's own source adapters and scoring rules, then suggests
   demo inputs that will show something interesting *right now*. Live data changes hour to hour,
   so run this shortly before presenting.

    .venv/bin/python scripts/preflight.py            # trips today
    .venv/bin/python scripts/preflight.py --days 1   # trips tomorrow
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import ai, airports, config, http  # noqa: E402
from app.geo import haversine_km  # noqa: E402
from app.models import Signal  # noqa: E402
from app.risk import LEVEL  # noqa: E402
from app.service import _run_source, build_context  # noqa: E402
from app.sources import faa, nws, open_meteo, sigmet, taf  # noqa: E402

HUBS = [
    "ATL", "AUS", "BNA", "BOS", "BWI", "CLT", "DCA", "DEN", "DFW", "DTW", "EWR", "IAD", "IAH", "JFK", "LAS",
    "LAX", "LGA", "MCO", "MDW", "MIA", "MSP", "ORD", "PDX", "PHL", "PHX", "SAN", "SEA", "SFO", "SLC", "TPA",
]
SHORT = {"faa": "FAA", "nws": "NWS", "taf": "TAF", "open_meteo": "Model"}
TTY = sys.stdout.isatty()
COLORS = {"ok": "32", "bad": "31", "warn": "33", "dim": "2", "bold": "1", 3: "31", 2: "33", 1: "32", 0: "2"}


def paint(text: str, key) -> str:
    return f"\033[{COLORS[key]}m{text}\033[0m" if TTY else text


def mark(ok) -> str:
    return paint("✓", "ok") if ok is True else paint("✗", "bad") if ok is False else paint("–", "dim")


async def check_ai() -> tuple[bool, str]:
    if not ai.enabled():
        return False, "ANTHROPIC_API_KEY not set: the app will use its rule-based fallback (no AI)"
    try:
        # models.retrieve validates the key and model names without spending tokens.
        ids = [(await ai._anthropic().models.retrieve(m)).id for m in (config.ANTHROPIC_MODEL, config.ANTHROPIC_PARSE_MODEL)]
    except Exception as e:  # noqa: BLE001
        return False, f"key or model problem: {type(e).__name__}: {str(e)[:160]}"
    return True, "key valid · models available: " + ", ".join(ids)


async def check_server(url: str) -> tuple[bool | None, str]:
    try:
        health = (await http.client().get(url.rstrip("/") + "/api/health", timeout=3)).json()
    except Exception:  # noqa: BLE001
        return None, f"not running at {url}  (start it with ./scripts/start.sh)"
    if health.get("ai_enabled"):
        return True, f"running at {url} · AI enabled ({health.get('model')})"
    return False, f"running at {url} but AI is DISABLED: check .env, then restart the server"


async def check_sigmets() -> tuple[bool, str]:
    t0 = time.perf_counter()
    try:
        data = await http.get_json(sigmet.URL, {"format": "json"}, ttl=300)
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e)[:120]}"
    convective = sum(
        1 for s in data if s.get("airSigmetType") == "SIGMET" and (s.get("hazard") or "").upper() == "CONVECTIVE"
    )
    ms = int((time.perf_counter() - t0) * 1000)
    return True, f"{ms} ms · {len(data)} active SIGMET/AIRMETs nationally ({convective} convective SIGMETs)"


async def scan(days: int):
    """Score every hub (plus any airport with a live FAA event) with the real adapters."""
    t0 = time.perf_counter()
    try:
        _, records = faa.parse_status_xml(await http.get_text(faa.URL, ttl=120))
    except Exception:  # noqa: BLE001 - the FAA adapter below reports the failure properly
        records = []
    faa_ms = int((time.perf_counter() - t0) * 1000)
    codes = list(dict.fromkeys(HUBS + [r["arpt"] for r in records if airports.by_iata(r["arpt"])]))
    group = [a for code in codes if (a := airports.by_iata(code))]
    now = datetime.now(timezone.utc)
    travel_date = now.astimezone(ZoneInfo(group[0].tz)).date() + timedelta(days=days)
    ctx = build_context(group[:-1], group[-1:], travel_date, now)
    results = await asyncio.gather(*(_run_source(m, ctx) for m in (faa, nws, taf, open_meteo)))
    # The adapter re-read the FAA feed from cache; report the real network time instead.
    if results[0].status.latency_ms is not None:
        results[0].status.latency_ms = max(results[0].status.latency_ms, faa_ms)
    return ctx, group, results


def summarize(group, results) -> tuple[dict[str, list[Signal]], dict[str, int], list[tuple[str, Signal]]]:
    scored: dict[str, list[Signal]] = {a.iata: [] for a in group}
    context: list[tuple[str, Signal]] = []
    for result in results:
        for s in result.signals:
            if s.category == "forecast":  # NWS text forecast is only fetched for two of the scanned airports
                continue
            for code in s.location.split(","):
                if code not in scored:
                    continue
                if s.counts_toward_score and s.severity > 0:
                    scored[code].append(s)
                elif s.source == "faa":
                    context.append((code, s))
    severity = {code: max((s.severity for s in sigs), default=0) for code, sigs in scored.items()}
    return scored, severity, context


def phrase(code: str) -> str:
    """City name if typing it resolves to this airport (e.g. 'Boston'), else the code (e.g. 'LGA')."""
    city = airports.by_iata(code).city.split(",")[0].strip()
    hit = airports.resolve_local(city)
    return city if hit and hit[0].iata == code else code


def dist_km(a: str, b: str) -> float:
    x, y = airports.by_iata(a), airports.by_iata(b)
    return haversine_km(x.lat, x.lon, y.lat, y.lon)


def expect(sev: int) -> str:
    return "expect HIGH" if sev == 3 else f"expect {LEVEL[sev]} or higher"


def suggestions(days: int, scored, severity, context) -> list[tuple[str, str]]:
    when = {0: "today", 1: "tomorrow"}.get(days, f"in {days} days")
    ranked = sorted((c for c in severity if severity[c] > 0), key=lambda c: (-severity[c], -len(scored[c]), c))
    impacted = [c for c in ranked if severity[c] >= 2]
    calm = [c for c in HUBS if severity.get(c) == 0]
    out: list[tuple[str, str]] = []
    used: set[str] = set()

    partner = next((c for c in impacted[1:] if dist_km(impacted[0], c) > 500), None) if impacted else None
    if partner:
        o = impacted[0]
        out.append((f"{phrase(o)} to {phrase(partner)} {when}",
                    f"{expect(max(severity[o], severity[partner]))}: live signals at both ends"))
    for o in impacted[:2]:
        d = max((c for c in calm if c not in used), key=lambda c: dist_km(o, c), default=None)
        if d:
            used.add(d)
            out.append((f"{phrase(o)} to {phrase(d)} {when}", f"{expect(severity[o])}: driven by {o} alone"))
    out.append(("New York to San Francisco tomorrow", "the brief's own example"))
    out.append(("Seattle to Miami in 30 days", "expect UNKNOWN: beyond every forecast horizon"))
    for code, s in context:
        far = max((c for c in calm if c != code), key=lambda c: dist_km(code, c), default=None)
        if far and phrase(code) != code:
            out.append((f"{phrase(code)} to {phrase(far)} {when}", f"{code} has an FAA item the app shows as context-only"))
            break
    return out


async def run(days: int, server: str) -> int:
    t0 = time.perf_counter()
    (ai_ok, ai_msg), (srv_ok, srv_msg), (sig_ok, sig_msg), (ctx, group, results) = await asyncio.gather(
        check_ai(), check_server(server), check_sigmets(), scan(days)
    )
    when = {0: "today", 1: "tomorrow"}.get(days, f"in {days} days")
    local_now = datetime.now().astimezone()
    print(paint(f"\nTravel Disruption Radar · pre-flight for trips {when} ({ctx.travel_date:%a %b %d})", "bold"))
    print(paint(f"Checked {local_now:%a %b %d %H:%M %Z}", "dim"))

    print(paint("\nDependencies", "bold"))
    print(f"  {mark(ai_ok)} {'Anthropic API':<27} {ai_msg}")
    failed = []
    for r in results:
        st = r.status
        ok = True if st.status == "ok" else False if st.status == "error" else None
        if ok is False:
            failed.append(st.name)
        latency = f"{st.latency_ms} ms · " if st.latency_ms is not None and ok else ""
        print(f"  {mark(ok)} {st.name:<27} {latency}{st.message.replace('at your airports', 'at scanned airports')}")
    if not sig_ok:
        failed.append("SIGMETs")
    print(f"  {mark(sig_ok)} {'AviationWeather.gov SIGMETs':<27} {sig_msg}")
    print(f"  {mark(srv_ok)} {'App server':<27} {srv_msg}")

    scored, severity, context = summarize(group, results)
    ranked = sorted((c for c in severity if severity[c] > 0), key=lambda c: (-severity[c], -len(scored[c]), c))
    print(paint(f"\nAirports with live signals ({len(group)} scanned, scored with the app's rules)", "bold"))
    if not ranked:
        print("  None right now. Demo the calm path and the uncertainty story, and use backup screenshots for a disruption.")
    for code in ranked[:12]:
        sigs = sorted(scored[code], key=lambda s: -s.severity)
        for i, s in enumerate(sigs[:3]):
            label = paint(f"{LEVEL[severity[code]]:<9}", severity[code]) if i == 0 else " " * 9
            print(f"  {label} {code if i == 0 else '':<4} [{SHORT.get(s.source, s.source)}] {s.title[:95]}")
    if len(ranked) > 12:
        rest = ranked[12:]
        print(paint(f"  … plus {len(rest)} more with LOW-level signals: {', '.join(rest)}", "dim"))

    if context:
        print(paint("\nContext-only FAA items (shown in the app but not scored, good for explaining relevance filtering)", "bold"))
        for code, s in context[:5]:
            print(f"  {code:<4} {s.title[:100]}")

    print(paint("\nSuggested demo inputs (type into the box at the top of the app)", "bold"))
    for i, (text, why) in enumerate(suggestions(days, scored, severity, context), 1):
        print(f"  {i}. {paint(repr(text).replace(chr(39), chr(34)), 'bold'):<45} {why}")

    elapsed = time.perf_counter() - t0
    if failed:
        print(paint(f"\n✗ Issues: {', '.join(failed)} unavailable. The app will still run; that source shows as an error "
                    f"and confidence drops one notch. ({elapsed:.1f}s)\n", "bad"))
        return 1
    note = "" if ai_ok else " (AI is off: the app will run in fallback mode)"
    print(paint(f"\n✓ Ready for the demo{note}. ({elapsed:.1f}s)\n", "ok" if ai_ok else "warn"))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Pre-flight check for the live demo.")
    parser.add_argument("--days", type=int, default=0, help="trip lead time to scan for: 0 = today (default), max 15")
    parser.add_argument("--server", default="http://127.0.0.1:8000", help="app URL to health-check")
    args = parser.parse_args()
    if not 0 <= args.days <= 15:
        parser.error("--days must be between 0 and 15")

    async def go() -> int:
        try:
            return await run(args.days, args.server)
        finally:
            await http.close()

    return asyncio.run(go())


if __name__ == "__main__":
    sys.exit(main())
