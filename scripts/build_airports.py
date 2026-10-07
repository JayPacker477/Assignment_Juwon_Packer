"""Build data/airports.json from the public OurAirports dataset.

Keeps US airports with scheduled airline service and resolves each airport's IANA
timezone via Open-Meteo (batched). Re-run occasionally; output is committed so the
app has no startup dependency on these services.

    python scripts/build_airports.py
"""

import csv
import io
import json
from pathlib import Path

import httpx

OURAIRPORTS_CSV = "https://davidmegginson.github.io/ourairports-data/airports.csv"
OPEN_METEO = "https://api.open-meteo.com/v1/forecast"
OUT = Path(__file__).resolve().parent.parent / "data" / "airports.json"


def main() -> None:
    with httpx.Client(timeout=60) as client:
        rows = csv.DictReader(io.StringIO(client.get(OURAIRPORTS_CSV).text))
        airports = [
            {
                "iata": r["iata_code"],
                "icao": r["icao_code"] or r["gps_code"] or r["ident"],
                "name": r["name"],
                "city": r["municipality"],
                "state": r["iso_region"].removeprefix("US-"),
                "lat": round(float(r["latitude_deg"]), 4),
                "lon": round(float(r["longitude_deg"]), 4),
                "size": "large" if r["type"] == "large_airport" else "medium",
            }
            for r in rows
            if r["iso_country"] == "US"
            and r["type"] in ("large_airport", "medium_airport")
            and r["scheduled_service"] == "yes"
            and r["iata_code"]
        ]

        for i in range(0, len(airports), 100):
            batch = airports[i : i + 100]
            resp = client.get(
                OPEN_METEO,
                params={
                    "latitude": ",".join(str(a["lat"]) for a in batch),
                    "longitude": ",".join(str(a["lon"]) for a in batch),
                    "timezone": "auto",
                    "forecast_days": 1,
                    "daily": "weather_code",
                },
            )
            resp.raise_for_status()
            data = resp.json()
            data = data if isinstance(data, list) else [data]
            for a, d in zip(batch, data):
                a["tz"] = d["timezone"]

    airports.sort(key=lambda a: (a["size"] != "large", a["iata"]))
    OUT.write_text(json.dumps(airports, indent=0))
    print(f"wrote {len(airports)} airports to {OUT}")


if __name__ == "__main__":
    main()
