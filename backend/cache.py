from __future__ import annotations

import copy
import threading
import time
from typing import Any, Dict, Optional, TypedDict


class CacheEntry(TypedDict):
    data: Any
    last_updated: Optional[int]
    last_error: Optional[str]
    last_error_at: Optional[int]
    fetch_count: int
    error_count: int


class Cache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._store: Dict[str, CacheEntry] = {}

    def _ensure_key(self, key: str) -> CacheEntry:
        if key not in self._store:
            self._store[key] = {
                "data": [],
                "last_updated": None,
                "last_error": None,
                "last_error_at": None,
                "fetch_count": 0,
                "error_count": 0,
            }
        return self._store[key]

    def set(self, key: str, data: Any) -> None:
        now = int(time.time())
        with self._lock:
            entry = self._ensure_key(key)
            entry["data"] = copy.deepcopy(data)
            entry["last_updated"] = now
            entry["last_error"] = None
            entry["last_error_at"] = None
            entry["fetch_count"] += 1

    def get(self, key: str) -> CacheEntry:
        with self._lock:
            entry = self._ensure_key(key)
            return {
                "data": copy.deepcopy(entry["data"]),
                "last_updated": entry["last_updated"],
                "last_error": entry["last_error"],
                "last_error_at": entry["last_error_at"],
                "fetch_count": entry["fetch_count"],
                "error_count": entry["error_count"],
            }

    def record_error(self, key: str, error: str) -> None:
        now = int(time.time())
        with self._lock:
            entry = self._ensure_key(key)
            entry["last_error"] = error
            entry["last_error_at"] = now
            entry["error_count"] += 1

    def get_all_metadata(self) -> Dict[str, CacheEntry]:
        with self._lock:
            return {
                key: {
                    "data": copy.deepcopy(entry["data"]),
                    "last_updated": entry["last_updated"],
                    "last_error": entry["last_error"],
                    "last_error_at": entry["last_error_at"],
                    "fetch_count": entry["fetch_count"],
                    "error_count": entry["error_count"],
                }
                for key, entry in self._store.items()
            }
