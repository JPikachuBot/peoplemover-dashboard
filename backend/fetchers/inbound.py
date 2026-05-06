from __future__ import annotations

import csv
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, TypedDict

from nyct_gtfs import NYCTFeed

from .mta import FeedStaleError, fetch_mta_feeds


logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parents[2]
STOPS_PATH = ROOT_DIR / "data" / "mta-static" / "stops.txt"


class DestinationConfig(TypedDict, total=False):
    name: str
    walk_time_minutes: int


class InboundConfig(TypedDict, total=False):
    enabled: bool
    label: str
    routes: List[str]
    direction: str
    stops: Dict[str, str]
    tracking_window: Dict[str, object]
    next_at_42: Dict[str, object]
    in_flight: Dict[str, object]
    destination_stations: List[DestinationConfig]
    building_buffer_minutes: int
    max_trains: int


class InboundNextAt42(TypedDict):
    trip_id: str
    route_id: str
    current_position: str
    gct_42_eta: int
    fulton_eta: Optional[int]
    wall_eta: Optional[int]


class InboundInFlight(TypedDict):
    trip_id: str
    route_id: str
    current_position: str
    fulton_eta: Optional[int]
    wall_eta: Optional[int]


class InboundPayload(TypedDict):
    next_at_42: List[InboundNextAt42]
    in_flight: List[InboundInFlight]


@dataclass(frozen=True)
class StopRow:
    stop_id: str
    stop_name: str
    location_type: str
    parent_station: str


@dataclass(frozen=True)
class StopTime:
    stop_id: str
    timestamp: int


@dataclass
class InboundCacheState:
    timestamp: Optional[int]
    payload: InboundPayload


_CACHE = InboundCacheState(timestamp=None, payload={"next_at_42": [], "in_flight": []})

_STOPS_CACHE: Optional[List[StopRow]] = None

_ORDINAL_PATTERN = re.compile(r"(\\d+)(st|nd|rd|th)\\b", re.IGNORECASE)

_PREFERRED_PARENT_STATIONS: Dict[str, Tuple[str, ...]] = {
    "59 st": ("629",),
    "grand central 42 st": ("631",),
    "14 st union sq": ("635",),
    "14 st union square": ("635",),
    "brooklyn bridge": ("640",),
    "brooklyn bridge city hall": ("640",),
    "wall st": ("419",),
    "fulton st": ("418",),
}

_LEX_SOUTHBOUND_CORRIDOR_PARENTS: Tuple[int, ...] = (
    631,  # Grand Central-42 St
    632,  # 33 St
    633,  # 28 St
    634,  # 23 St-Baruch College
    635,  # 14 St-Union Sq
    636,  # Astor Pl
    637,  # Bleecker St
    638,  # Spring St
    639,  # Canal St
    640,  # Brooklyn Bridge-City Hall
    418,  # Fulton St
    419,  # Wall St
)

_LEX_SOUTHBOUND_INDEX = {parent: index for index, parent in enumerate(_LEX_SOUTHBOUND_CORRIDOR_PARENTS)}


class InboundConfigError(RuntimeError):
    pass


def _load_stops() -> List[StopRow]:
    global _STOPS_CACHE
    if _STOPS_CACHE is not None:
        return _STOPS_CACHE
    if not STOPS_PATH.exists():
        raise FileNotFoundError(f"Stops file not found: {STOPS_PATH}")
    stops: List[StopRow] = []
    with STOPS_PATH.open(newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            stop_id = row.get("stop_id", "").strip()
            stop_name = row.get("stop_name", "").strip()
            if not stop_id or not stop_name:
                continue
            stops.append(
                StopRow(
                    stop_id=stop_id,
                    stop_name=stop_name,
                    location_type=row.get("location_type", "").strip(),
                    parent_station=row.get("parent_station", "").strip(),
                )
            )
    _STOPS_CACHE = stops
    return stops


def _normalize_station_name(value: str) -> str:
    cleaned = value.lower()
    cleaned = cleaned.replace("–", "-").replace("—", "-")
    cleaned = cleaned.replace("/", " ")
    cleaned = re.sub(r"[^a-z0-9\s-]", "", cleaned)
    cleaned = _ORDINAL_PATTERN.sub(r"\1", cleaned)
    cleaned = re.sub(r"[-\s]+", " ", cleaned)
    return cleaned.strip()


def _names_match(candidate: str, target: str) -> bool:
    candidate_norm = _normalize_station_name(candidate)
    target_norm = _normalize_station_name(target)
    return (
        candidate_norm == target_norm
        or candidate_norm.startswith(target_norm)
        or target_norm.startswith(candidate_norm)
    )


def _find_stop_candidates(stops: Iterable[StopRow], name: str, direction: str) -> List[StopRow]:
    desired_suffix = direction.upper()
    matches = [
        stop
        for stop in stops
        if _names_match(stop.stop_name, name)
        and stop.location_type != "1"
        and stop.stop_id.upper().endswith(desired_suffix)
    ]
    return matches


def _resolve_stop_id(
    name: str,
    direction: str,
    stops: Sequence[StopRow],
) -> str:
    matches = _find_stop_candidates(stops, name, direction)
    if not matches:
        raise InboundConfigError(f"No stop_id found for {name} ({direction}).")

    normalized_name = _normalize_station_name(name)
    preferred_parents = _PREFERRED_PARENT_STATIONS.get(normalized_name)
    if preferred_parents:
        preferred = [stop for stop in matches if stop.parent_station in preferred_parents]
        if preferred:
            matches = preferred

    matches.sort(key=lambda stop: stop.stop_id)
    return matches[0].stop_id


def _extract_config(config: Mapping[str, object]) -> InboundConfig:
    inbound_raw = config.get("inbound_tracker", {})
    if inbound_raw is None:
        return {}
    if not isinstance(inbound_raw, dict):
        raise InboundConfigError("inbound_tracker must be a mapping.")
    return inbound_raw


def _extract_stop_times(trip: object) -> List[StopTime]:
    updates = getattr(trip, "stop_time_updates", None)
    if not updates:
        return []
    stop_times: List[StopTime] = []
    for update in updates:
        stop_id = getattr(update, "stop_id", None)
        if not stop_id:
            continue
        arrival_dt = getattr(update, "arrival", None) or getattr(update, "departure", None)
        if arrival_dt is None:
            continue
        stop_times.append(StopTime(stop_id=str(stop_id), timestamp=int(arrival_dt.timestamp())))
    stop_times.sort(key=lambda item: item.timestamp)
    return stop_times


def _build_stop_name_lookup(stops: Iterable[StopRow]) -> Dict[str, str]:
    return {stop.stop_id: stop.stop_name for stop in stops}


def _build_stop_row_lookup(stops: Iterable[StopRow]) -> Dict[str, StopRow]:
    return {stop.stop_id: stop for stop in stops}


def _parent_station_number(stop: StopRow) -> Optional[int]:
    try:
        return int(stop.parent_station)
    except (TypeError, ValueError):
        return None


def _format_current_position(
    stop_times: Sequence[StopTime],
    stop_name_lookup: Mapping[str, str],
    now_timestamp: int,
) -> str:
    next_stop = next((stop for stop in stop_times if stop.timestamp >= now_timestamp), None)
    if next_stop:
        name = stop_name_lookup.get(next_stop.stop_id, next_stop.stop_id)
        return f"Approaching {name}"
    if stop_times:
        last_stop = stop_times[-1]
        name = stop_name_lookup.get(last_stop.stop_id, last_stop.stop_id)
        return f"At {name}"
    return "In transit"


def _next_stop(stop_times: Sequence[StopTime], now_timestamp: int) -> Optional[StopTime]:
    return next((stop for stop in stop_times if stop.timestamp >= now_timestamp), None)


def _previous_stop(stop_times: Sequence[StopTime], now_timestamp: int) -> Optional[StopTime]:
    previous: Optional[StopTime] = None
    for stop in stop_times:
        if stop.timestamp > now_timestamp:
            break
        previous = stop
    return previous


def _minutes_until(timestamp: int, now_timestamp: int) -> int:
    if timestamp <= now_timestamp:
        return 0
    return max(0, int((timestamp - now_timestamp) // 60))


def _safe_int(value: object, default: int, minimum: int = 0) -> int:
    try:
        cast = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, cast)


def _parent_station_for_stop_id(
    stop_id: str,
    stop_row_lookup: Mapping[str, StopRow],
) -> Optional[int]:
    row = stop_row_lookup.get(stop_id)
    if row is None:
        return None
    return _parent_station_number(row)


def _is_between_parent_stations(
    candidate_parent: Optional[int],
    start_parent: Optional[int],
    end_parent: Optional[int],
) -> bool:
    if candidate_parent is None or start_parent is None or end_parent is None:
        return False
    start_index = _LEX_SOUTHBOUND_INDEX.get(start_parent)
    end_index = _LEX_SOUTHBOUND_INDEX.get(end_parent)
    candidate_index = _LEX_SOUTHBOUND_INDEX.get(candidate_parent)
    if start_index is not None and end_index is not None:
        if candidate_index is None:
            return False
        if start_index < end_index:
            return start_index < candidate_index <= end_index
        if start_index > end_index:
            return end_index <= candidate_index < start_index
        return False
    lower = min(start_parent, end_parent)
    upper = max(start_parent, end_parent)
    return lower <= candidate_parent <= upper and candidate_parent != start_parent


def _inflight_parent_heuristic(
    last_parent: Optional[int],
    next_parent: Optional[int],
    start_parent: Optional[int],
    end_parent: Optional[int],
) -> bool:
    if _is_between_parent_stations(next_parent, start_parent, end_parent):
        return True
    if _is_between_parent_stations(last_parent, start_parent, end_parent):
        return True
    if (
        last_parent == start_parent
        and _is_between_parent_stations(next_parent, start_parent, end_parent)
    ):
        return True
    if (
        next_parent == end_parent
        and _is_between_parent_stations(last_parent, start_parent, end_parent)
    ):
        return True
    return False


def fetch_inbound_trains(
    config: Mapping[str, object],
    feeds_by_name: Optional[Dict[str, NYCTFeed]] = None,
    now_timestamp: Optional[int] = None,
) -> InboundPayload:
    inbound_config = _extract_config(config)
    enabled = inbound_config.get("enabled", True)
    if enabled is False:
        return {"next_at_42": [], "in_flight": []}

    routes = inbound_config.get("routes", [])
    if not isinstance(routes, list) or not routes:
        return {"next_at_42": [], "in_flight": []}
    route_set = {str(route).strip() for route in routes if str(route).strip()}
    if not route_set:
        return {"next_at_42": [], "in_flight": []}

    direction = str(inbound_config.get("direction", "S")).strip().upper()
    if direction not in {"N", "S"}:
        raise InboundConfigError("Inbound direction must be N or S.")

    tracking_window = inbound_config.get("tracking_window", {})
    if not isinstance(tracking_window, dict):
        raise InboundConfigError("tracking_window must be a mapping.")

    # New semantics (preferred): start_station/end_station + include_next_at_start
    start_station = tracking_window.get("start_station")
    end_station = tracking_window.get("end_station")
    include_next_at_start = tracking_window.get("include_next_at_start", 0)

    # Backward compatibility with previous north_boundary/south_boundary config
    if not isinstance(start_station, str):
        start_station = tracking_window.get("north_boundary")
    if not isinstance(end_station, str):
        end_station = tracking_window.get("south_boundary")

    if not isinstance(start_station, str) or not start_station.strip():
        raise InboundConfigError("tracking_window must include start_station.")
    if not isinstance(end_station, str) or not end_station.strip():
        raise InboundConfigError("tracking_window must include end_station.")

    include_next_at_start_int = _safe_int(include_next_at_start, default=0, minimum=0)

    next_at_42_config = inbound_config.get("next_at_42", {})
    in_flight_config = inbound_config.get("in_flight", {})
    if not isinstance(next_at_42_config, dict):
        next_at_42_config = {}
    if not isinstance(in_flight_config, dict):
        in_flight_config = {}

    destination_stations = inbound_config.get("destination_stations", [])
    if not isinstance(destination_stations, list):
        raise InboundConfigError("destination_stations must be a list.")

    now_timestamp = now_timestamp if now_timestamp is not None else int(time.time())

    if feeds_by_name is None:
        api_key = os.environ.get("MTA_API_KEY")
        feeds_by_name = fetch_mta_feeds(["gtfs-1234567"], api_key=api_key, now_timestamp=now_timestamp)

    feed = feeds_by_name.get("gtfs-1234567") if feeds_by_name else None
    if feed is None:
        raise FeedStaleError("GTFS feed gtfs-1234567 unavailable for inbound tracker.")

    stops = _load_stops()
    stop_name_lookup = _build_stop_name_lookup(stops)
    stop_row_lookup = _build_stop_row_lookup(stops)

    stops_config = inbound_config.get("stops", {})
    if not isinstance(stops_config, dict):
        stops_config = {}

    def _stop_from_config(key: str) -> str:
        value = stops_config.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else ""

    start_stop_id = _stop_from_config("gct_42") or _resolve_stop_id(start_station, direction, stops)
    end_stop_id = _stop_from_config("wall") or _resolve_stop_id(end_station, direction, stops)

    destination_map: Dict[str, str] = {}
    for destination in destination_stations:
        if not isinstance(destination, dict):
            continue
        name = destination.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        stop_id = _resolve_stop_id(name, direction, stops)
        destination_map[_normalize_station_name(name)] = stop_id

    wall_stop_id = (
        _stop_from_config("wall")
        or destination_map.get(_normalize_station_name("Wall St"), "")
        or _resolve_stop_id("Wall St", direction, stops)
    )
    fulton_stop_id = (
        _stop_from_config("fulton")
        or destination_map.get(_normalize_station_name("Fulton St"), "")
        or _resolve_stop_id("Fulton St", direction, stops)
    )
    if not wall_stop_id or not fulton_stop_id or not start_stop_id:
        raise InboundConfigError(
            "Inbound stops missing: ensure gct_42, Fulton St, and Wall St stop IDs are resolved."
        )

    next_at_42_max_default = include_next_at_start_int or 2
    next_at_42_max = _safe_int(
        next_at_42_config.get("max_trains"),
        default=next_at_42_max_default,
        minimum=0,
    )
    in_flight_max_default = _safe_int(inbound_config.get("max_trains"), default=8, minimum=1)
    in_flight_max = _safe_int(in_flight_config.get("max_trains"), default=in_flight_max_default, minimum=1)

    inflight: List[InboundInFlight] = []
    upcoming_at_start: List[InboundNextAt42] = []

    inbound_debug = os.environ.get("INBOUND_DEBUG") == "1"

    start_parent = _parent_station_for_stop_id(start_stop_id, stop_row_lookup)
    end_parent = _parent_station_for_stop_id(end_stop_id, stop_row_lookup)

    for trip in feed.filter_trips():
        route_id = getattr(trip, "route_id", None)
        trip_id = getattr(trip, "trip_id", None)
        if not route_id or not trip_id:
            continue
        if str(route_id) not in route_set:
            continue

        stop_times = _extract_stop_times(trip)
        if not stop_times:
            continue

        stop_time_lookup = {stop.stop_id: stop.timestamp for stop in stop_times}
        start_time = stop_time_lookup.get(start_stop_id)
        wall_time = stop_time_lookup.get(wall_stop_id)
        fulton_time = stop_time_lookup.get(fulton_stop_id)

        wall_eta = _minutes_until(wall_time, now_timestamp) if wall_time and wall_time > now_timestamp else None
        if fulton_time and fulton_time > now_timestamp:
            fulton_eta: Optional[int] = _minutes_until(fulton_time, now_timestamp)
        else:
            fulton_eta = None

        if fulton_eta is not None and wall_eta is not None and fulton_eta >= wall_eta:
            fulton_eta = None

        current_position = _format_current_position(stop_times, stop_name_lookup, now_timestamp)
        next_stop = _next_stop(stop_times, now_timestamp)
        last_stop = _previous_stop(stop_times, now_timestamp)
        next_stop_parent = (
            _parent_station_for_stop_id(next_stop.stop_id, stop_row_lookup) if next_stop else None
        )
        last_stop_parent = (
            _parent_station_for_stop_id(last_stop.stop_id, stop_row_lookup) if last_stop else None
        )
        next_stop_name = (
            stop_name_lookup.get(next_stop.stop_id, next_stop.stop_id) if next_stop else ""
        )
        north_station_flag = "86 st" in next_stop_name.lower() or "138 st" in next_stop_name.lower()
        parent_outside_corridor = False
        if next_stop_parent is not None and next_stop_parent not in _LEX_SOUTHBOUND_INDEX:
            parent_outside_corridor = True
        if last_stop_parent is not None and last_stop_parent not in _LEX_SOUTHBOUND_INDEX:
            parent_outside_corridor = True

        wall_time_valid = wall_time is not None and wall_time > now_timestamp

        # Bucket A: trains that have left start_station but have not arrived at end_station yet.
        if wall_time_valid and start_time is not None and start_time <= now_timestamp < wall_time:
            if inbound_debug and (north_station_flag or parent_outside_corridor):
                logger.debug(
                    "Inbound inflight (time window) trip %s route %s next_stop=%s last_parent=%s "
                    "next_parent=%s start_parent=%s end_parent=%s",
                    trip_id,
                    route_id,
                    next_stop_name or "unknown",
                    last_stop_parent,
                    next_stop_parent,
                    start_parent,
                    end_parent,
                )
            inflight.append(
                {
                    "trip_id": str(trip_id),
                    "route_id": str(route_id),
                    "current_position": current_position,
                    "fulton_eta": fulton_eta,
                    "wall_eta": wall_eta,
                }
            )
            continue

        # If the start stop_time_update is missing or inconsistent, fall back to parent-station ordering.
        if wall_time_valid and _inflight_parent_heuristic(
            last_stop_parent,
            next_stop_parent,
            start_parent,
            end_parent,
        ):
            if inbound_debug and (north_station_flag or parent_outside_corridor):
                logger.debug(
                    "Inbound inflight (heuristic) trip %s route %s next_stop=%s last_parent=%s "
                    "next_parent=%s start_parent=%s end_parent=%s",
                    trip_id,
                    route_id,
                    next_stop_name or "unknown",
                    last_stop_parent,
                    next_stop_parent,
                    start_parent,
                    end_parent,
                )
            inflight.append(
                {
                    "trip_id": str(trip_id),
                    "route_id": str(route_id),
                    "current_position": current_position,
                    "fulton_eta": fulton_eta,
                    "wall_eta": wall_eta,
                }
            )
            continue

        # Bucket B: next trains approaching start_station (optional).
        if next_at_42_max > 0 and start_time is not None and start_time > now_timestamp:
            start_eta = _minutes_until(start_time, now_timestamp)
            upcoming_at_start.append(
                {
                    "trip_id": str(trip_id),
                    "route_id": str(route_id),
                    "current_position": current_position,
                    "gct_42_eta": start_eta,
                    "fulton_eta": fulton_eta,
                    "wall_eta": wall_eta,
                }
            )

    inflight.sort(key=lambda item: item["wall_eta"] if item["wall_eta"] is not None else 999999)
    upcoming_at_start.sort(key=lambda item: item["gct_42_eta"])

    inflight = inflight[:in_flight_max]
    upcoming_at_start = upcoming_at_start[:next_at_42_max]

    payload: InboundPayload = {"next_at_42": upcoming_at_start, "in_flight": inflight}

    _CACHE.timestamp = now_timestamp
    _CACHE.payload = dict(payload)
    return payload
