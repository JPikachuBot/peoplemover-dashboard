from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

WMATA_BASE = "https://api.wmata.com"
REQUEST_TIMEOUT = 15

LINE_COLORS: Dict[str, str] = {
    "OR": "#F27B26",
    "SV": "#A2AAAD",
    "BL": "#00A1E0",
    "RD": "#E51636",
    "GR": "#00A859",
    "YL": "#FFD700",
}

# WMATA sometimes returns these as line codes for non-revenue trains
_SKIP_LINES = {"No", "N/A", "--"}


def _parse_minutes(raw: Any) -> Optional[int]:
    """Convert WMATA Min field to integer. ARR/BRD → 0. Unknown → None."""
    if raw in ("ARR", "BRD"):
        return 0
    if raw in ("---", "--", None, ""):
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


def _haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 3958.8
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _walk_minutes(dist_miles: float, speed_mph: float = 3.0) -> int:
    return max(1, round((dist_miles / speed_mph) * 60))


def fetch_wmata_arrivals(
    config: Dict[str, Any],
    api_key: str,
    now_timestamp: int,
) -> List[Dict[str, Any]]:
    """
    Returns normalized transit data:
    [
      {
        station_id, station_name, provider, lines, walk_minutes,
        directions: [
          { label, arrivals: [{line, line_color, destination, minutes, cars, status}] }
        ]
      }
    ]
    """
    transit = config.get("transit", {})
    stations_cfg = transit.get("stations", [])
    location = config.get("location", {})
    home_lat = location.get("lat")
    home_lng = location.get("lng")

    results: List[Dict[str, Any]] = []

    for station in stations_cfg:
        if not isinstance(station, dict):
            continue

        station_code = station.get("station_code", "").strip()
        if not station_code:
            logger.warning("Station %s missing station_code; skipping.", station.get("id"))
            continue

        direction_cfgs: List[Dict[str, Any]] = station.get("directions", [])

        try:
            resp = requests.get(
                f"{WMATA_BASE}/StationPrediction.svc/json/GetPredictions/{station_code}",
                headers={"api_key": api_key},
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            raw_trains: List[Dict[str, Any]] = resp.json().get("Trains", [])
        except requests.HTTPError as exc:
            logger.error("WMATA HTTP error for %s: %s", station_code, exc)
            raw_trains = []
        except Exception as exc:
            logger.error("WMATA fetch error for %s: %s", station_code, exc)
            raw_trains = []

        # Group arrivals by group number ("1" or "2")
        by_group: Dict[str, List[Dict[str, Any]]] = {}
        for train in raw_trains:
            line = str(train.get("Line", "")).strip()
            if line in _SKIP_LINES or not line:
                continue
            minutes = _parse_minutes(train.get("Min"))
            if minutes is None:
                continue
            group = str(train.get("Group", "1")).strip()
            by_group.setdefault(group, []).append({
                "line": line,
                "line_color": LINE_COLORS.get(line, "#888888"),
                "destination": train.get("DestinationName", ""),
                "minutes": minutes,
                "cars": train.get("Car"),
                "status": "ARR" if train.get("Min") in ("ARR", "BRD") else None,
            })

        # Sort each group by minutes
        for group in by_group:
            by_group[group].sort(key=lambda a: a["minutes"])

        # Map config directions to arrivals
        directions: List[Dict[str, Any]] = []
        for d_cfg in direction_cfgs:
            group = str(d_cfg.get("group", "1")).strip()
            directions.append({
                "label": d_cfg.get("label", f"Group {group}"),
                "arrivals": by_group.get(group, []),
            })

        # Walk time
        walk: Optional[int] = None
        st_lat = station.get("lat")
        st_lng = station.get("lng")
        if all(v is not None for v in [home_lat, home_lng, st_lat, st_lng]):
            dist = _haversine_miles(home_lat, home_lng, st_lat, st_lng)
            walk = _walk_minutes(dist)

        results.append({
            "station_id": station.get("id", station_code),
            "station_name": station.get("name", station_code),
            "provider": "wmata",
            "lines": station.get("lines", []),
            "walk_minutes": walk,
            "directions": directions,
        })

    return results
