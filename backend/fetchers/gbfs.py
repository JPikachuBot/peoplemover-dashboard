from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10


def _fetch_json(url: str) -> Any:
    resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _discover_feed_urls(manifest_url: str) -> Dict[str, str]:
    """Parse a GBFS manifest and return {feed_name: url} for all available feeds."""
    try:
        manifest = _fetch_json(manifest_url)
    except Exception as exc:
        raise RuntimeError(f"Failed to fetch GBFS manifest at {manifest_url}: {exc}") from exc

    feeds: Dict[str, str] = {}
    data = manifest.get("data", {})
    # GBFS 2.x: data.en.feeds / data.feeds; GBFS 3.x: data.feeds
    for lang_or_key, value in data.items():
        if isinstance(value, dict) and "feeds" in value:
            for feed in value["feeds"]:
                feeds[feed["name"]] = feed["url"]
            break
        if lang_or_key == "feeds" and isinstance(value, list):
            for feed in value:
                feeds[feed["name"]] = feed["url"]
            break

    if not feeds:
        # Fallback: derive URLs from manifest URL base
        base = manifest_url.rsplit("/", 1)[0]
        feeds["station_information"] = f"{base}/en/station_information.json"
        feeds["station_status"] = f"{base}/en/station_status.json"

    return feeds


def fetch_gbfs_status(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Returns list of bikeshare station dicts:
    [{ station_id, name, bikes_available, e_bikes_available, docks_available,
       is_renting, is_returning }]

    Station selection modes (from config.bikeshare):
      - stations: [{station_id, name}]  — explicit list by ID
      - station_name_search: [str]       — fuzzy name match from GBFS feed
    If neither is set, all stations are returned (not recommended for large systems).
    """
    bikeshare_cfg = config.get("bikeshare", {})
    if not isinstance(bikeshare_cfg, dict):
        return []

    feed_url = bikeshare_cfg.get("feed_url", "")
    if not feed_url:
        raise ValueError("bikeshare.feed_url is required.")

    explicit_stations: List[Dict[str, Any]] = bikeshare_cfg.get("stations", [])
    name_search: List[str] = bikeshare_cfg.get("station_name_search", [])

    try:
        feed_urls = _discover_feed_urls(feed_url)
    except Exception as exc:
        logger.error("GBFS discovery failed: %s", exc)
        raise

    info_url = feed_urls.get("station_information", "")
    status_url = feed_urls.get("station_status", "")

    if not info_url or not status_url:
        raise ValueError("GBFS feed missing station_information or station_status URLs.")

    try:
        info_payload = _fetch_json(info_url)
        status_payload = _fetch_json(status_url)
    except Exception as exc:
        logger.error("GBFS fetch failed: %s", exc)
        raise

    info_by_id: Dict[str, Dict[str, Any]] = {
        str(s["station_id"]): s
        for s in info_payload.get("data", {}).get("stations", [])
    }
    status_by_id: Dict[str, Dict[str, Any]] = {
        str(s["station_id"]): s
        for s in status_payload.get("data", {}).get("stations", [])
    }

    # Determine which station IDs to include
    target_ids: Optional[set] = None

    if explicit_stations:
        # Mode 1: explicit station ID list
        target_ids = {str(s["station_id"]) for s in explicit_stations if s.get("station_id")}
    elif name_search:
        # Mode 2: name-based search
        terms = [t.lower() for t in name_search if t.strip()]
        target_ids = {
            sid
            for sid, info in info_by_id.items()
            if any(term in info.get("name", "").lower() for term in terms)
        }

    results: List[Dict[str, Any]] = []
    for station_id, status in status_by_id.items():
        if target_ids is not None and station_id not in target_ids:
            continue

        info = info_by_id.get(station_id, {})
        name = info.get("name") or next(
            (s.get("name", station_id) for s in explicit_stations
             if str(s.get("station_id")) == station_id),
            station_id,
        )

        results.append({
            "station_id": station_id,
            "name": name,
            "bikes_available": int(status.get("num_bikes_available", 0)),
            "e_bikes_available": int(status.get("num_ebikes_available", 0)),
            "docks_available": int(status.get("num_docks_available", 0)),
            "is_renting": bool(status.get("is_renting", True)),
            "is_returning": bool(status.get("is_returning", True)),
        })

    results.sort(key=lambda s: s["name"])
    return results
