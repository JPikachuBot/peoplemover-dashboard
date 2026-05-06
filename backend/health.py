from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

try:
    from .cache import Cache
except ImportError:
    from cache import Cache  # type: ignore[no-redef]

START_TIME = time.time()


def _format_age(last_updated: Optional[int], now: int) -> str:
    if not last_updated:
        return "never"
    return f"{max(0, now - last_updated)}s ago"


def _source_status(
    last_updated: Optional[int],
    last_error_at: Optional[int],
    now: int,
    warning_sec: int,
    critical_sec: int,
) -> str:
    if last_error_at and (last_updated is None or last_error_at >= last_updated):
        return "error"
    if last_updated is None:
        return "error"
    age = now - last_updated
    if age >= critical_sec:
        return "error"
    if age >= warning_sec:
        return "stale"
    return "healthy"


def get_health_status(
    cache: Cache,
    active_keys: List[str],
    staleness_warning_sec: int,
    staleness_critical_sec: int,
) -> Dict[str, Any]:
    now = int(time.time())
    metadata = cache.get_all_metadata()

    sources: Dict[str, Any] = {}
    for key in active_keys:
        entry = metadata.get(key, {})
        status = _source_status(
            last_updated=entry.get("last_updated"),
            last_error_at=entry.get("last_error_at"),
            now=now,
            warning_sec=staleness_warning_sec,
            critical_sec=staleness_critical_sec,
        )
        sources[key] = {
            "last_update": _format_age(entry.get("last_updated"), now),
            "status": status,
            "fetch_count": int(entry.get("fetch_count", 0)),
            "error_count": int(entry.get("error_count", 0)),
        }

    statuses = [s["status"] for s in sources.values()]
    if "error" in statuses:
        overall = "down"
    elif "stale" in statuses:
        overall = "degraded"
    else:
        overall = "healthy"

    return {
        "status": overall,
        "uptime_seconds": int(now - START_TIME),
        "sources": sources,
    }
