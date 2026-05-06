from __future__ import annotations

import logging
from typing import Any, Dict, List

import requests

logger = logging.getLogger(__name__)

TOMTOM_ROUTING_URL = "https://api.tomtom.com/routing/1/calculateRoute"
REQUEST_TIMEOUT = 15


def fetch_traffic(config: Dict[str, Any], api_key: str) -> List[Dict[str, Any]]:
    """
    Returns list of route dicts:
    [{ id, label, provider, duration_minutes, duration_no_traffic_minutes, delay_minutes,
       error? }]
    """
    traffic_cfg = config.get("traffic", {})
    routes_cfg: List[Dict[str, Any]] = traffic_cfg.get("routes", [])
    results: List[Dict[str, Any]] = []

    for route in routes_cfg:
        route_id = route.get("id", "")
        label = route.get("label", route_id)
        origin = route.get("origin", {})
        destination = route.get("destination", {})

        orig_str = f"{origin.get('lat')},{origin.get('lng')}"
        dest_str = f"{destination.get('lat')},{destination.get('lng')}"

        try:
            resp = requests.get(
                f"{TOMTOM_ROUTING_URL}/{orig_str}:{dest_str}/json",
                params={
                    "key": api_key,
                    "traffic": "true",
                    "travelMode": "car",
                    "departAt": "now",
                },
                timeout=REQUEST_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()

            summary = data["routes"][0]["summary"]
            travel_sec = int(summary["travelTimeInSeconds"])
            no_traffic_sec = int(summary.get("noTrafficTravelTimeInSeconds", travel_sec))

            results.append({
                "id": route_id,
                "label": label,
                "provider": "tomtom",
                "duration_minutes": round(travel_sec / 60),
                "duration_no_traffic_minutes": round(no_traffic_sec / 60),
                "delay_minutes": max(0, round((travel_sec - no_traffic_sec) / 60)),
                "summary": "",
            })

        except Exception as exc:
            logger.error("TomTom fetch error for route %s: %s", route_id, exc)
            results.append({"id": route_id, "label": label, "provider": "tomtom",
                             "error": str(exc)})

    return results
