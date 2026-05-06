from __future__ import annotations

import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, send_from_directory

try:
    from flask_cors import CORS
except ImportError:
    CORS = None

from backend.cache import Cache
from backend.health import get_health_status
from backend.fetchers.wmata import fetch_wmata_arrivals
from backend.fetchers.gbfs import fetch_gbfs_status
from backend.fetchers.mta import (
    fetch_mta_feeds,
    fetch_subway_arrivals,
    get_required_feeds_from_config,
)
from backend.fetchers.inbound import fetch_inbound_trains

ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / "config.yaml"

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
if CORS is not None:
    CORS(app)

cache = Cache()

MTA_LINE_COLORS: Dict[str, str] = {
    "1": "#EE352E", "2": "#EE352E", "3": "#EE352E",
    "4": "#00933C", "5": "#00933C", "6": "#00933C",
    "7": "#B933AD",
    "A": "#0039A6", "C": "#0039A6", "E": "#0039A6",
    "B": "#FF6319", "D": "#FF6319", "F": "#FF6319", "M": "#FF6319",
    "G": "#6CBE45",
    "J": "#996633", "Z": "#996633",
    "L": "#A7A9AC",
    "N": "#FCCC0A", "Q": "#FCCC0A", "R": "#FCCC0A", "W": "#FCCC0A",
    "S": "#808183",
}


# ── Config ────────────────────────────────────────────────────────────


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config not found: {CONFIG_PATH}")
    with CONFIG_PATH.open() as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ValueError("Config root must be a mapping.")
    return data


def _safe_int(value: Any, fallback: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(value))
    except (TypeError, ValueError):
        return fallback


def _safe_float(value: Any) -> Optional[float]:
    try:
        return None if value is None else float(value)
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


# ── Provider detection ────────────────────────────────────────────────


def _transit_provider(config: Dict[str, Any]) -> str:
    return config.get("transit", {}).get("provider", "mta")


def _traffic_provider(config: Dict[str, Any]) -> Optional[str]:
    traffic = config.get("traffic")
    return traffic.get("provider") if isinstance(traffic, dict) else None


def _bikeshare_provider(config: Dict[str, Any]) -> Optional[str]:
    bs = config.get("bikeshare")
    return bs.get("provider") if isinstance(bs, dict) else None


def _active_cache_keys(config: Dict[str, Any]) -> List[str]:
    keys = ["transit"]
    if _traffic_provider(config):
        keys.append("traffic")
    if _bikeshare_provider(config):
        keys.append("bikeshare")
    return keys


# ── MTA normalization ─────────────────────────────────────────────────


def _normalize_mta_transit(
    raw_arrivals: List[Dict[str, Any]],
    config: Dict[str, Any],
    home_lat: Optional[float],
    home_lng: Optional[float],
) -> List[Dict[str, Any]]:
    """Convert flat MTA arrivals list into normalized station/direction format."""
    transit = config.get("transit", {})
    stations_cfg: List[Dict[str, Any]] = transit.get("stations", []) if isinstance(transit, dict) else []
    elevator_buffer = _safe_int(transit.get("elevator_buffer_minutes", 0), 0)

    by_key: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for arrival in raw_arrivals:
        key = (str(arrival["station_block_id"]), str(arrival["direction"]))
        by_key.setdefault(key, []).append(arrival)

    results: List[Dict[str, Any]] = []
    for station in stations_cfg:
        if not isinstance(station, dict):
            continue
        station_id = station.get("id", "")
        directions_out: List[Dict[str, Any]] = []

        for d in station.get("directions", []):
            if not isinstance(d, dict):
                continue
            code = str(d.get("code", "")).upper()
            label = d.get("destination") or d.get("label", code)
            arrivals_raw = sorted(
                by_key.get((station_id, code), []),
                key=lambda a: a["minutes_until"],
            )
            arrivals = [
                {
                    "line": a["line"],
                    "line_color": MTA_LINE_COLORS.get(a["line"], "#888888"),
                    "destination": a.get("direction_destination"),
                    "minutes": a["minutes_until"],
                    "cars": None,
                    "status": None,
                }
                for a in arrivals_raw
            ]
            directions_out.append({"label": label, "arrivals": arrivals})

        walk: Optional[int] = None
        st_lat = _safe_float(station.get("lat"))
        st_lng = _safe_float(station.get("lng"))
        if all(v is not None for v in [home_lat, home_lng, st_lat, st_lng]):
            dist = _haversine_miles(home_lat, home_lng, st_lat, st_lng)  # type: ignore[arg-type]
            walk = _walk_minutes(dist) + elevator_buffer

        results.append({
            "station_id": station_id,
            "station_name": station.get("name", station_id),
            "provider": "mta",
            "lines": station.get("lines", []),
            "walk_minutes": walk,
            "directions": directions_out,
        })

    return results


# ── Frontend config ───────────────────────────────────────────────────


def _build_frontend_config(config: Dict[str, Any]) -> Dict[str, Any]:
    location = config.get("location", {}) if isinstance(config.get("location"), dict) else {}
    display = config.get("display", {}) if isinstance(config.get("display"), dict) else {}
    transit = config.get("transit", {}) if isinstance(config.get("transit"), dict) else {}
    traffic = config.get("traffic", {}) if isinstance(config.get("traffic"), dict) else {}
    bikeshare = config.get("bikeshare", {}) if isinstance(config.get("bikeshare"), dict) else {}

    home_lat = _safe_float(location.get("lat"))
    home_lng = _safe_float(location.get("lng"))
    provider = transit.get("provider", "mta")
    elevator_buffer = _safe_int(transit.get("elevator_buffer_minutes", 0), 0)

    stations_out: List[Dict[str, Any]] = []
    for station in transit.get("stations", []):
        if not isinstance(station, dict):
            continue
        st_lat = _safe_float(station.get("lat"))
        st_lng = _safe_float(station.get("lng"))
        walk: Optional[int] = None
        if all(v is not None for v in [home_lat, home_lng, st_lat, st_lng]):
            dist = _haversine_miles(home_lat, home_lng, st_lat, st_lng)  # type: ignore[arg-type]
            walk = _walk_minutes(dist) + elevator_buffer

        directions_out: List[Dict[str, Any]] = []
        for d in station.get("directions", []):
            if not isinstance(d, dict):
                continue
            if provider == "wmata":
                directions_out.append({"group": str(d.get("group", "1")), "label": d.get("label", "")})
            else:
                directions_out.append({
                    "code": str(d.get("code", "")).upper(),
                    "label": d.get("label", ""),
                    "stop_id": d.get("stop_id", ""),
                })

        stations_out.append({
            "id": station.get("id", ""),
            "name": station.get("name", ""),
            "lines": station.get("lines", []),
            "walk_minutes": walk,
            "directions": directions_out,
        })

    traffic_out: Optional[Dict[str, Any]] = None
    if traffic:
        routes_out = [
            {"id": r.get("id", ""), "label": r.get("label", "")}
            for r in traffic.get("routes", [])
            if isinstance(r, dict)
        ]
        traffic_out = {"provider": traffic.get("provider"), "routes": routes_out}

    bikeshare_out: Optional[Dict[str, Any]] = None
    if bikeshare:
        bikeshare_out = {"provider": bikeshare.get("provider")}

    return {
        "location": {"name": location.get("name"), "lat": home_lat, "lng": home_lng},
        "transit": {"provider": provider, "stations": stations_out},
        "traffic": traffic_out,
        "bikeshare": bikeshare_out,
        "display": {
            "refresh_interval_ms": _safe_int(display.get("refresh_interval_ms", 15000), 15000, 1000),
            "staleness_warning_sec": _safe_int(display.get("staleness_warning_sec", 60), 60),
            "staleness_critical_sec": _safe_int(display.get("staleness_critical_sec", 120), 120),
            "theme": display.get("theme", "dark"),
            "orientation": display.get("orientation", "landscape"),
        },
    }


# ── Poll intervals ────────────────────────────────────────────────────


def _poll_intervals(config: Dict[str, Any]) -> Tuple[int, int, int]:
    transit = config.get("transit", {}) if isinstance(config.get("transit"), dict) else {}
    traffic = config.get("traffic", {}) if isinstance(config.get("traffic"), dict) else {}
    bikeshare = config.get("bikeshare", {}) if isinstance(config.get("bikeshare"), dict) else {}
    return (
        _safe_int(transit.get("poll_interval_seconds", 30), 30, 5),
        _safe_int(traffic.get("poll_interval_seconds", 300), 300, 30),
        _safe_int(bikeshare.get("poll_interval_seconds", 60), 60, 10),
    )


# ── Scheduled tasks ───────────────────────────────────────────────────


def fetch_transit_task() -> None:
    try:
        config = load_config()
        provider = _transit_provider(config)
        now = int(time.time())

        if provider == "wmata":
            api_key = os.environ.get("WMATA_API_KEY", "")
            if not api_key:
                logger.warning("WMATA_API_KEY not set; WMATA requests will fail.")
            data = fetch_wmata_arrivals(config, api_key, now)
            cache.set("transit", data)
            logger.info("Transit (WMATA): fetched %d stations", len(data))

        elif provider == "mta":
            api_key = os.environ.get("MTA_API_KEY")
            feed_names = get_required_feeds_from_config(config)
            feeds = fetch_mta_feeds(feed_names, api_key, now)
            raw = fetch_subway_arrivals(config, feeds_by_name=feeds, now_timestamp=now)

            location = config.get("location", {})
            home_lat = _safe_float(location.get("lat"))
            home_lng = _safe_float(location.get("lng"))
            data = _normalize_mta_transit(raw, config, home_lat, home_lng)
            cache.set("transit", data)
            logger.info("Transit (MTA): fetched %d arrivals across %d stations", len(raw), len(data))

            # Inbound tracker (MTA-specific, optional)
            inbound_cfg = config.get("inbound_tracker", {})
            if isinstance(inbound_cfg, dict) and inbound_cfg.get("enabled", True) and inbound_cfg.get("routes"):
                inbound = fetch_inbound_trains(config, feeds_by_name=feeds, now_timestamp=now)
                cache.set("inbound", inbound)
                logger.info(
                    "Inbound: %d next_at_start, %d in_flight",
                    len(inbound.get("next_at_42", [])),
                    len(inbound.get("in_flight", [])),
                )

        else:
            logger.error("Unknown transit provider: %s", provider)

    except Exception as exc:
        cache.record_error("transit", str(exc))
        logger.error("Transit fetch failed: %s", exc, exc_info=True)


def fetch_traffic_task() -> None:
    try:
        config = load_config()
        provider = _traffic_provider(config)
        if not provider:
            return
        api_key = os.environ.get("TRAFFIC_API_KEY", "")
        if not api_key:
            logger.warning("TRAFFIC_API_KEY not set; traffic requests will fail.")

        if provider == "google_maps":
            from backend.fetchers.traffic.google_maps import fetch_traffic
        elif provider == "tomtom":
            from backend.fetchers.traffic.tomtom import fetch_traffic
        else:
            logger.error("Unknown traffic provider: %s", provider)
            return

        data = fetch_traffic(config, api_key)
        cache.set("traffic", data)
        logger.info("Traffic (%s): fetched %d routes", provider, len(data))

    except Exception as exc:
        cache.record_error("traffic", str(exc))
        logger.error("Traffic fetch failed: %s", exc, exc_info=True)


def fetch_bikeshare_task() -> None:
    try:
        config = load_config()
        provider = _bikeshare_provider(config)
        if not provider:
            return

        if provider == "gbfs":
            data = fetch_gbfs_status(config)
            cache.set("bikeshare", data)
            logger.info("Bikeshare (GBFS): fetched %d stations", len(data))
        else:
            logger.error("Unknown bikeshare provider: %s", provider)

    except Exception as exc:
        cache.record_error("bikeshare", str(exc))
        logger.error("Bikeshare fetch failed: %s", exc, exc_info=True)


# ── Helpers ───────────────────────────────────────────────────────────


def _staleness(last_updated: Any, now: int) -> Optional[int]:
    if not isinstance(last_updated, int):
        return None
    return max(0, now - last_updated)


def _fmt_iso(ts: Optional[int]) -> Optional[str]:
    if not isinstance(ts, int):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── API endpoints ─────────────────────────────────────────────────────


@app.route("/")
def index() -> Any:
    return send_from_directory("../frontend", "index.html")


@app.route("/<path:path>")
def static_files(path: str) -> Any:
    return send_from_directory("../frontend", path)


@app.route("/api/transit")
def api_transit() -> Any:
    entry = cache.get("transit")
    now = int(time.time())
    return jsonify({
        "success": True,
        "data": entry["data"],
        "last_updated": entry["last_updated"],
        "staleness_seconds": _staleness(entry["last_updated"], now),
    })


# Backward-compat alias
@app.route("/api/subway")
def api_subway() -> Any:
    return api_transit()


@app.route("/api/traffic")
def api_traffic() -> Any:
    entry = cache.get("traffic")
    now = int(time.time())
    return jsonify({
        "success": True,
        "data": entry["data"],
        "last_updated": entry["last_updated"],
        "staleness_seconds": _staleness(entry["last_updated"], now),
    })


@app.route("/api/bikeshare")
def api_bikeshare() -> Any:
    entry = cache.get("bikeshare")
    now = int(time.time())
    return jsonify({
        "success": True,
        "data": entry["data"],
        "last_updated": entry["last_updated"],
        "staleness_seconds": _staleness(entry["last_updated"], now),
    })


# Backward-compat alias
@app.route("/api/citibike")
def api_citibike() -> Any:
    return api_bikeshare()


@app.route("/api/inbound")
def api_inbound() -> Any:
    """MTA-specific inbound tracker. Returns empty payload if not configured."""
    entry = cache.get("inbound")
    data = entry.get("data") or {}
    if not isinstance(data, dict):
        data = {}
    return jsonify({
        "next_at_42": data.get("next_at_42", []),
        "in_flight": data.get("in_flight", []),
        "last_updated": _fmt_iso(entry["last_updated"]),
    })


@app.route("/api/config")
def api_config() -> Any:
    config = load_config()
    return jsonify({"success": True, "data": _build_frontend_config(config)})


@app.route("/api/health")
@app.route("/health")
def api_health() -> Any:
    config = load_config()
    display = config.get("display", {}) if isinstance(config.get("display"), dict) else {}
    warning = _safe_int(display.get("staleness_warning_sec", 60), 60)
    critical = _safe_int(display.get("staleness_critical_sec", 120), 120)
    active_keys = _active_cache_keys(config)
    status = get_health_status(cache, active_keys, warning, critical)
    return jsonify(status)


# ── Startup ───────────────────────────────────────────────────────────


def main() -> None:
    try:
        config = load_config()
    except Exception as exc:
        logger.error("Failed to load config: %s", exc)
        return

    transit_interval, traffic_interval, bikeshare_interval = _poll_intervals(config)
    traffic_provider = _traffic_provider(config)
    bikeshare_provider = _bikeshare_provider(config)

    scheduler = BackgroundScheduler()
    scheduler.add_job(fetch_transit_task, "interval", seconds=transit_interval)
    if traffic_provider:
        scheduler.add_job(fetch_traffic_task, "interval", seconds=traffic_interval)
    if bikeshare_provider:
        scheduler.add_job(fetch_bikeshare_task, "interval", seconds=bikeshare_interval)
    scheduler.start()

    logger.info("Initial data fetch...")
    fetch_transit_task()
    if traffic_provider:
        fetch_traffic_task()
    if bikeshare_provider:
        fetch_bikeshare_task()

    logger.info(
        "Scheduler: transit every %ds, traffic every %ds, bikeshare every %ds",
        transit_interval,
        traffic_interval if traffic_provider else 0,
        bikeshare_interval if bikeshare_provider else 0,
    )

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "5000"))
    logger.info("Server starting on http://%s:%s", host, port)
    app.run(host=host, port=port)


if __name__ == "__main__":
    main()
