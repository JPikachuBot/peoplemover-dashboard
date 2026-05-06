from __future__ import annotations

import logging
from typing import Any, Dict, List

import requests

logger = logging.getLogger(__name__)

GMAPS_DIRECTIONS_URL = "https://maps.googleapis.com/maps/api/directions/json"
REQUEST_TIMEOUT = 15


def fetch_traffic(config: Dict[str, Any], api_key: str) -> List[Dict[str, Any]]:
    """
    Returns list of route dicts:
    [{ id, label, provider, duration_minutes, duration_no_traffic_minutes, delay_minutes,
       summary, error? }]
    """
    traffic_cfg = config.get("traffic", {})
    routes_cfg: List[Dict[str, Any]] = traffic_cfg.get("routes", [])
    results: List[Dict[str, Any]] = []

    for route in routes_cfg:
        route_id = route.get("id", "")
        label = route.get("label", route_id)
        origin = route.get("origin", {})
        destination = route.get("destination", {})

        # Prefer address string; fall back to lat,lng
        origin_str = origin.get("address") or f"{origin.get('lat')},{origin.get('lng')}"
        dest_str = destination.get("address") or f"{destination.get('lat')},{destination.get('lng')}"

        try:
            resp = requests.get(
                GMAPS_DIRECTIONS_URL,
                params={
                    "origin": origin_str,
                    "destination": dest_str,
                    "departure_time": "now",
                    "traffic_model": "best_guess",
                    "key": api_key,
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            status = data.get("status")
            if status != "OK":
                logger.warning("Google Maps returned status %s for route %s", status, route_id)
                results.append({"id": route_id, "label": label, "provider": "google_maps",
                                 "error": status})
                continue

            leg = data["routes"][0]["legs"][0]
            duration_sec = leg["duration"]["value"]
            duration_traffic_sec = leg.get("duration_in_traffic", {}).get("value", duration_sec)

            results.append({
                "id": route_id,
                "label": label,
                "provider": "google_maps",
                "duration_minutes": round(duration_traffic_sec / 60),
                "duration_no_traffic_minutes": round(duration_sec / 60),
                "delay_minutes": max(0, round((duration_traffic_sec - duration_sec) / 60)),
                "summary": leg.get("summary", ""),
            })

        except Exception as exc:
            logger.error("Google Maps fetch error for route %s: %s", route_id, exc)
            results.append({"id": route_id, "label": label, "provider": "google_maps",
                             "error": str(exc)})

    return results
