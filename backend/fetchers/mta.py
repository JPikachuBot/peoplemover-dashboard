from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple, TypedDict

import requests
import yaml
from nyct_gtfs import NYCTFeed


ROOT_DIR = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT_DIR / "config.yaml"

FEED_URLS: Dict[str, str] = {
    "gtfs-1234567": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs",
    "gtfs-ace": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-ace",
    "gtfs-nqrw": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-nqrw",
    "gtfs-jz": "https://api-endpoint.mta.info/Dataservice/mtagtfsfeeds/nyct%2Fgtfs-jz",
}

LINE_TO_FEED: Dict[str, str] = {
    "1": "gtfs-1234567",
    "2": "gtfs-1234567",
    "3": "gtfs-1234567",
    "4": "gtfs-1234567",
    "5": "gtfs-1234567",
    "6": "gtfs-1234567",
    "7": "gtfs-1234567",
    "A": "gtfs-ace",
    "C": "gtfs-ace",
    "E": "gtfs-ace",
    "J": "gtfs-jz",
    "Z": "gtfs-jz",
    "N": "gtfs-nqrw",
    "Q": "gtfs-nqrw",
    "R": "gtfs-nqrw",
    "W": "gtfs-nqrw",
}

FEED_TIMEOUT_SECONDS = 30
STALE_THRESHOLD_SECONDS = 120

logger = logging.getLogger(__name__)

UPTOWN_LINES: Set[str] = {
    "1",
    "2",
    "3",
    "4",
    "5",
    "6",
    "A",
    "C",
    "E",
    "B",
    "D",
    "F",
    "M",
    "N",
    "Q",
    "R",
    "W",
}


class Arrival(TypedDict):
    line: str
    station: str
    station_block_id: str
    direction: str
    direction_label: str
    direction_destination: Optional[str]
    minutes_until: int
    route_id: str
    stop_id: str
    timestamp: int


class DirectionConfig(TypedDict, total=False):
    code: str
    label: str
    destination: str
    stop_id: str


class StationBlockConfig(TypedDict, total=False):
    id: str
    name: str
    lines: List[str]
    directions: List[DirectionConfig]
    stop_id: str
    direction: str


@dataclass
class ArrivalCandidate:
    arrival_timestamp: int
    line: str
    station: str
    station_block_id: str
    direction: str
    direction_label: str
    direction_destination: Optional[str]
    route_id: str
    stop_id: str


@dataclass(frozen=True)
class DirectionSpec:
    code: str
    label: str
    destination: Optional[str]
    stop_id: str


@dataclass(frozen=True)
class StationBlock:
    id: str
    name: str
    lines: List[str]
    directions: List[DirectionSpec]


@dataclass(frozen=True)
class StationDirection:
    station_block_id: str
    station_name: str
    lines: List[str]
    direction_code: str
    direction_label: str
    direction_destination: Optional[str]
    stop_id: str


@dataclass
class CacheState:
    timestamp: Optional[int]
    arrivals: List[Arrival]


_CACHE = CacheState(timestamp=None, arrivals=[])


class FeedStaleError(RuntimeError):
    pass


def load_config(config_path: Path = CONFIG_PATH) -> dict:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    with config_path.open() as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError("Config root must be a mapping.")
    return data


def get_required_feeds(stations: Sequence[StationBlock]) -> List[str]:
    required: Set[str] = set()
    for station in stations:
        for line in station.lines:
            feed = LINE_TO_FEED.get(line)
            if feed is None:
                logger.warning("Unknown line '%s' in config; skipping feed mapping.", line)
                continue
            required.add(feed)
    ordered = [feed for feed in FEED_URLS.keys() if feed in required]
    return ordered


def minutes_until(arrival_timestamp: int, now_timestamp: int) -> int:
    if arrival_timestamp <= now_timestamp:
        return 0
    return max(0, int((arrival_timestamp - now_timestamp) // 60))


def _fetch_feed(feed_name: str, api_key: Optional[str], timeout_seconds: int) -> NYCTFeed:
    feed_url = FEED_URLS[feed_name]
    feed = NYCTFeed(feed_url, fetch_immediately=False)
    headers = {"x-api-key": api_key} if api_key else None
    response = requests.get(
        feed_url,
        headers=headers,
        timeout=timeout_seconds,
    )
    if response.status_code in {401, 403}:
        logger.error(
            "MTA feed request unauthorized for %s (HTTP %s).",
            feed_name,
            response.status_code,
        )
    response.raise_for_status()
    feed.load_gtfs_bytes(response.content)
    return feed


def _is_stop_id_valid(feed: NYCTFeed, stop_id: str) -> bool:
    try:
        feed._stops.get_station_name(stop_id)
    except ValueError:
        return False
    return True


def _collect_candidates(
    feed: NYCTFeed,
    stations: Sequence[StationDirection],
    now_timestamp: int,
) -> Dict[int, List[ArrivalCandidate]]:
    candidates_by_station: Dict[int, List[ArrivalCandidate]] = {
        idx: [] for idx in range(len(stations))
    }

    for idx, station in enumerate(stations):
        stop_id = station.stop_id
        if not _is_stop_id_valid(feed, stop_id):
            logger.warning(
                "Invalid stop_id '%s' in feed; skipping station %s.",
                stop_id,
                station.station_name,
            )
            continue

        # NOTE: Do NOT filter by line_id here. Service changes can temporarily route
        # other lines over this stop (e.g., N serving the R/W local). Filtering only
        # by configured lines would hide valid arrivals.
        trips = feed.filter_trips()
        for trip in trips:
            for update in trip.stop_time_updates:
                if update.stop_id != stop_id:
                    continue
                arrival_dt = update.arrival or update.departure
                if arrival_dt is None:
                    continue
                arrival_ts = int(arrival_dt.timestamp())
                if arrival_ts < now_timestamp:
                    continue
                candidates_by_station[idx].append(
                    ArrivalCandidate(
                        arrival_timestamp=arrival_ts,
                        line=trip.route_id,
                        station=station.station_name,
                        station_block_id=station.station_block_id,
                        direction=station.direction_code,
                        direction_label=station.direction_label,
                        direction_destination=station.direction_destination,
                        route_id=trip.route_id,
                        stop_id=stop_id,
                    )
                )

    return candidates_by_station


def _select_arrivals(
    candidates_by_station: Dict[int, List[ArrivalCandidate]],
    now_timestamp: int,
    fetched_timestamp: int,
) -> List[Arrival]:
    results: List[Arrival] = []
    for idx in sorted(candidates_by_station.keys()):
        candidates = candidates_by_station[idx]
        candidates.sort(key=lambda candidate: candidate.arrival_timestamp)

        seen: Set[Tuple[str, str, int]] = set()
        kept: List[ArrivalCandidate] = []
        for candidate in candidates:
            key = (candidate.route_id, candidate.stop_id, candidate.arrival_timestamp)
            if key in seen:
                continue
            seen.add(key)
            kept.append(candidate)
            if len(kept) >= 2:
                break

        for candidate in kept:
            results.append(
                Arrival(
                    line=candidate.line,
                    station=candidate.station,
                    station_block_id=candidate.station_block_id,
                    direction=candidate.direction,
                    direction_label=candidate.direction_label,
                    direction_destination=candidate.direction_destination,
                    minutes_until=minutes_until(candidate.arrival_timestamp, now_timestamp),
                    route_id=candidate.route_id,
                    stop_id=candidate.stop_id,
                    timestamp=fetched_timestamp,
                )
            )
    return results


def _slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", value.strip().lower())
    return cleaned.strip("_") or "station"


def _normalize_station_block_id(name: str, lines: List[str], provided: Optional[str]) -> str:
    if isinstance(provided, str) and provided.strip():
        return provided.strip()
    line_key = "_".join(sorted(_slugify(line) for line in lines if line))
    return f"{_slugify(name)}_{line_key}".strip("_")


def _resolve_direction_label(lines: List[str], code: str, provided: Optional[str]) -> str:
    if isinstance(provided, str) and provided.strip():
        return provided.strip()
    direction_code = code.strip().upper()
    normalized_lines = {str(line).strip().upper() for line in lines if str(line).strip()}
    is_uptown = any(line in UPTOWN_LINES for line in normalized_lines)
    if direction_code in {"N", "S"} and is_uptown:
        return "Uptown" if direction_code == "N" else "Downtown"
    if direction_code == "N":
        return "Northbound"
    if direction_code == "S":
        return "Southbound"
    if direction_code == "E":
        return "Eastbound"
    if direction_code == "W":
        return "Westbound"
    return "Unknown direction"


def _extract_station_blocks(config: dict) -> List[StationBlock]:
    subway = config.get("subway")
    if not isinstance(subway, dict):
        raise ValueError("Config missing subway section.")
    stations = subway.get("stations")
    if not isinstance(stations, list):
        raise ValueError("Config subway.stations must be a list.")
    normalized: List[StationBlock] = []
    for station_raw in stations:
        if not isinstance(station_raw, dict):
            raise ValueError("Each station entry must be a mapping.")
        name = station_raw.get("name")
        lines = station_raw.get("lines")
        stop_id = station_raw.get("stop_id")
        direction = station_raw.get("direction")
        directions = station_raw.get("directions")
        if not isinstance(name, str) or not isinstance(lines, list):
            raise ValueError("Station entry missing name or lines.")
        normalized_lines = [str(line).strip() for line in lines if str(line).strip()]
        if not normalized_lines:
            raise ValueError(f"Station {name} missing lines.")

        direction_specs: List[DirectionSpec] = []
        if isinstance(directions, list) and directions:
            for direction_raw in directions:
                if not isinstance(direction_raw, dict):
                    raise ValueError(f"Station {name} has invalid directions entry.")
                code = direction_raw.get("code")
                dir_stop_id = direction_raw.get("stop_id")
                label = direction_raw.get("label")
                destination = direction_raw.get("destination")
                if not isinstance(code, str) or not code.strip():
                    raise ValueError(f"Station {name} direction missing code.")
                if not isinstance(dir_stop_id, str) or not dir_stop_id.strip():
                    raise ValueError(f"Station {name} direction {code} missing stop_id.")
                resolved_label = _resolve_direction_label(normalized_lines, code, label)
                resolved_destination = (
                    destination.strip()
                    if isinstance(destination, str) and destination.strip()
                    else None
                )
                direction_specs.append(
                    DirectionSpec(
                        code=code.strip().upper(),
                        label=resolved_label,
                        destination=resolved_destination,
                        stop_id=dir_stop_id.strip(),
                    )
                )
        elif isinstance(stop_id, str) and stop_id.strip() and isinstance(direction, str) and direction.strip():
            resolved_label = _resolve_direction_label(normalized_lines, direction, None)
            direction_specs.append(
                DirectionSpec(
                    code=direction.strip().upper(),
                    label=resolved_label,
                    destination=None,
                    stop_id=stop_id.strip(),
                )
            )
        else:
            raise ValueError(f"Station {name} missing directions/stop_id configuration.")

        station_block_id = _normalize_station_block_id(
            name,
            normalized_lines,
            station_raw.get("id"),
        )
        normalized.append(
            StationBlock(
                id=station_block_id,
                name=name.strip(),
                lines=normalized_lines,
                directions=direction_specs,
            )
        )
    return normalized


def _expand_station_directions(stations: Sequence[StationBlock]) -> List[StationDirection]:
    expanded: List[StationDirection] = []
    for station in stations:
        for direction in station.directions:
            expanded.append(
                StationDirection(
                    station_block_id=station.id,
                    station_name=station.name,
                    lines=station.lines,
                    direction_code=direction.code,
                    direction_label=direction.label,
                    direction_destination=direction.destination,
                    stop_id=direction.stop_id,
                )
            )
    return expanded


def fetch_mta_feeds(
    feed_names: Sequence[str],
    api_key: Optional[str],
    now_timestamp: Optional[int] = None,
) -> Dict[str, NYCTFeed]:
    timestamp = now_timestamp if now_timestamp is not None else int(time.time())
    feeds: Dict[str, NYCTFeed] = {}
    for feed_name in feed_names:
        feed = _fetch_feed(feed_name, api_key, FEED_TIMEOUT_SECONDS)
        feed_timestamp = int(feed.last_generated.timestamp())
        if timestamp - feed_timestamp > STALE_THRESHOLD_SECONDS:
            raise FeedStaleError(
                f"Feed {feed_name} is stale: generated at {feed.last_generated}."
            )
        feeds[feed_name] = feed
    return feeds


def get_required_feeds_from_config(config: dict) -> List[str]:
    stations = _extract_station_blocks(config)
    return get_required_feeds(stations)


def fetch_subway_arrivals(
    config: dict,
    feeds_by_name: Optional[Dict[str, NYCTFeed]] = None,
    now_timestamp: Optional[int] = None,
) -> List[Arrival]:
    stations = _extract_station_blocks(config)
    station_directions = _expand_station_directions(stations)
    feed_names = get_required_feeds(stations)
    api_key = os.environ.get("MTA_API_KEY")

    if not api_key:
        logger.info("MTA_API_KEY is not set; fetching feeds without authentication.")

    now_timestamp = now_timestamp if now_timestamp is not None else int(time.time())
    fetched_timestamp = now_timestamp
    candidates_by_station: Dict[int, List[ArrivalCandidate]] = {
        idx: [] for idx in range(len(station_directions))
    }

    try:
        if feeds_by_name is None:
            feeds_by_name = fetch_mta_feeds(feed_names, api_key, now_timestamp)
        for feed_name in feed_names:
            feed = feeds_by_name.get(feed_name) if feeds_by_name else None
            if feed is None:
                logger.warning("Feed %s missing from provided feed cache.", feed_name)
                continue
            feed_candidates = _collect_candidates(feed, station_directions, now_timestamp)
            for idx, candidates in feed_candidates.items():
                candidates_by_station[idx].extend(candidates)
    except requests.RequestException as exc:
        logger.error("Network error while fetching MTA feed: %s", exc)
        return list(_CACHE.arrivals) if _CACHE.arrivals else []
    except FeedStaleError as exc:
        logger.warning("%s Returning cached data if available.", exc)
        return list(_CACHE.arrivals) if _CACHE.arrivals else []
    except Exception as exc:  # Explicit catch to keep fetcher resilient
        logger.error("Unexpected error while fetching MTA feed: %s", exc)
        return list(_CACHE.arrivals) if _CACHE.arrivals else []

    arrivals = _select_arrivals(candidates_by_station, now_timestamp, fetched_timestamp)
    _CACHE.timestamp = fetched_timestamp
    _CACHE.arrivals = arrivals
    return arrivals


def _format_station_header(station: StationBlock) -> str:
    lines_display = "/".join(station.lines)
    return f"{station.name.upper()} ({lines_display})"


def _render_output(stations: Sequence[StationBlock], arrivals: Sequence[Arrival]) -> str:
    arrivals_by_station: Dict[str, Dict[str, List[Arrival]]] = {}
    for arrival in arrivals:
        block_id = arrival["station_block_id"]
        direction = arrival["direction"]
        arrivals_by_station.setdefault(block_id, {}).setdefault(direction, []).append(arrival)

    output_lines: List[str] = []
    output_lines.append("Fetching MTA subway arrivals...")
    output_lines.append(f"Loaded {len(stations)} stations from config.yaml")

    required_feeds = get_required_feeds(stations)
    display_feeds = ", ".join(feed.replace("gtfs-", "").upper() for feed in required_feeds) or "NONE"
    output_lines.append(f"Fetching feeds: {display_feeds}")
    output_lines.append("")
    output_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    for station in stations:
        output_lines.append(_format_station_header(station))
        station_arrivals = arrivals_by_station.get(station.id, {})
        if not station_arrivals:
            output_lines.append("  (no upcoming trains)")
            output_lines.append("")
            continue
        for direction in station.directions:
            direction_arrivals = station_arrivals.get(direction.code, [])
            output_lines.append(f"  {direction.label}:")
            if not direction_arrivals:
                output_lines.append("    (no upcoming trains)")
                continue
            for arrival in direction_arrivals:
                output_lines.append(
                    f"    {arrival['line']} train → {arrival['minutes_until']} min"
                )
        output_lines.append("")

    output_lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    output_lines.append(f"Fetched {len(arrivals)} arrivals")
    if arrivals:
        last_updated = datetime.fromtimestamp(arrivals[0]["timestamp"])
        output_lines.append(f"Last updated: {last_updated.strftime('%Y-%m-%d %H:%M:%S')}")
    return "\n".join(output_lines)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        config = load_config(CONFIG_PATH)
    except Exception as exc:
        logger.error("Failed to load config: %s", exc)
        return

    stations = _extract_station_blocks(config)
    arrivals = fetch_subway_arrivals(config)
    output = _render_output(stations, arrivals)
    print(output)


if __name__ == "__main__":
    main()
