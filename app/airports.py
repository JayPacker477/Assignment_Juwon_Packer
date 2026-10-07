"""Resolve user-entered places ("NYC", "San Francisco", "JFK", "Boise, ID") to airports."""

from __future__ import annotations

import difflib
import json
import re
from functools import lru_cache
from typing import Optional

from . import http
from .config import ROOT
from .geo import haversine_km
from .models import Airport

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

# Multi-airport metros: the first code is treated as the default/primary airport.
METROS: dict[str, list[str]] = {
    "new york": ["JFK", "LGA", "EWR"],
    "san francisco": ["SFO", "OAK", "SJC"],
    "los angeles": ["LAX", "BUR", "LGB", "SNA", "ONT"],
    "chicago": ["ORD", "MDW"],
    "washington": ["DCA", "IAD", "BWI"],
    "houston": ["IAH", "HOU"],
    "dallas": ["DFW", "DAL"],
    "miami": ["MIA", "FLL"],
    "orlando": ["MCO", "SFB"],
    "tampa": ["TPA", "PIE"],
    "phoenix": ["PHX", "AZA"],
}
METRO_ALIASES: dict[str, str] = {
    "nyc": "new york", "new york city": "new york", "ny": "new york", "manhattan": "new york",
    "sf": "san francisco", "bay area": "san francisco", "san fran": "san francisco",
    "la": "los angeles", "l.a.": "los angeles",
    "chi": "chicago",
    "dc": "washington", "d.c.": "washington", "washington dc": "washington",
    "washington d.c.": "washington",
    "dallas fort worth": "dallas", "dallas-fort worth": "dallas", "dfw area": "dallas",
}


@lru_cache(maxsize=1)
def _airports() -> tuple[Airport, ...]:
    data = json.loads((ROOT / "data" / "airports.json").read_text())
    return tuple(Airport(**a) for a in data)


@lru_cache(maxsize=1)
def _by_iata() -> dict[str, Airport]:
    return {a.iata: a for a in _airports()}


def by_iata(code: str) -> Optional[Airport]:
    return _by_iata().get(code.upper())


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().lower())


def _city_key(a: Airport) -> str:
    return _norm(a.city.split(",")[0])


def _rank(airports: list[Airport]) -> list[Airport]:
    return sorted(airports, key=lambda a: a.size != "large")


def resolve_local(query: str) -> list[Airport]:
    """Resolve without network access. Returns primary airport first, or []."""
    q = _norm(query)
    if not q:
        return []
    code = q.upper()
    if len(code) == 3 and code in _by_iata():
        return [_by_iata()[code]]
    if len(code) == 4:
        for a in _airports():
            if a.icao == code:
                return [a]

    metro = METRO_ALIASES.get(q, q)
    if metro in METROS:
        return [a for c in METROS[metro] if (a := by_iata(c))]

    state = None
    m = re.match(r"^(.*?),\s*([a-z]{2})$", q)
    if m:
        q, state = m.group(1), m.group(2).upper()
    matches = [a for a in _airports() if _city_key(a) == q and (state is None or a.state == state)]
    if matches:
        large = [a for a in matches if a.size == "large"]
        return large or matches

    cities = sorted({_city_key(a) for a in _airports()})
    close = difflib.get_close_matches(q, cities, n=1, cutoff=0.85)
    if close:
        matches = [a for a in _airports() if _city_key(a) == close[0]]
        return [a for a in matches if a.size == "large"] or matches
    return []


def nearest(lat: float, lon: float, max_km: float = 150, limit: int = 3) -> list[Airport]:
    scored = [(haversine_km(lat, lon, a.lat, a.lon), a) for a in _airports()]
    near = sorted((s for s in scored if s[0] <= max_km), key=lambda s: (s[1].size != "large", s[0]))
    return [a for _, a in near[:limit]]


async def resolve(query: str) -> tuple[list[Airport], Optional[str]]:
    """Returns (airports, note). Falls back to Open-Meteo geocoding for small towns."""
    local = resolve_local(query)
    if local:
        return local, None
    try:
        data = await http.get_json(
            GEOCODE_URL, {"name": query.split(",")[0].strip(), "count": 5, "countryCode": "US"}, ttl=86400
        )
    except Exception:  # noqa: BLE001 - geocoding is best-effort
        return [], None
    for place in data.get("results") or []:
        near = nearest(place["latitude"], place["longitude"])
        if near:
            where = ", ".join(p for p in (place.get("name"), place.get("admin1")) if p)
            return near, f"'{query}' geocoded to {where}; using nearest airline airports."
    return [], None


def search(prefix: str, limit: int = 8) -> list[Airport]:
    p = _norm(prefix)
    if not p:
        return []
    hits = [
        a for a in _airports()
        if a.iata.lower().startswith(p) or _city_key(a).startswith(p) or p in a.name.lower()
    ]
    return _rank(hits)[:limit]
