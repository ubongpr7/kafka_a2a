from __future__ import annotations

import os
import threading
import time
from collections import Counter
from typing import Any
from uuid import uuid4


KA2A_TRACE_METADATA_KEY = "urn:ka2a:trace"

_LOCK = threading.Lock()
_COUNTERS: Counter[str] = Counter()
_TIMINGS: dict[str, dict[str, float]] = {}


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    value = value.strip().lower()
    if value in ("1", "true", "yes", "y", "on"):
        return True
    if value in ("0", "false", "no", "n", "off"):
        return False
    return default


def metrics_enabled(env: dict[str, str] | None = None) -> bool:
    env_map = env or os.environ
    return _parse_bool(env_map.get("KA2A_METRICS_ENABLED"), default=False)


def tracing_enabled(env: dict[str, str] | None = None) -> bool:
    env_map = env or os.environ
    return _parse_bool(env_map.get("KA2A_TRACE_ENABLED"), default=False)


def inc_counter(name: str, *, by: int = 1) -> None:
    if not metrics_enabled():
        return
    if not name:
        return
    with _LOCK:
        _COUNTERS[name] += int(by)


def observe_timing(name: str, *, seconds: float) -> None:
    if not metrics_enabled():
        return
    if not name:
        return
    seconds = float(max(0.0, seconds))
    with _LOCK:
        agg = _TIMINGS.setdefault(name, {"count": 0.0, "sum": 0.0, "max": 0.0})
        agg["count"] += 1.0
        agg["sum"] += seconds
        agg["max"] = max(agg["max"], seconds)


class Timer:
    def __init__(self, name: str):
        self._name = name
        self._start = time.monotonic()

    def stop(self) -> None:
        observe_timing(self._name, seconds=time.monotonic() - self._start)


def metrics_snapshot() -> dict[str, Any]:
    with _LOCK:
        return {
            "counters": dict(_COUNTERS),
            "timings": {k: dict(v) for k, v in _TIMINGS.items()},
        }


def new_trace_id() -> str:
    return str(uuid4())


def trace_id_from_metadata(metadata: dict[str, Any] | None) -> str | None:
    if not metadata:
        return None
    trace = metadata.get(KA2A_TRACE_METADATA_KEY)
    if isinstance(trace, str) and trace.strip():
        return trace.strip()
    if isinstance(trace, dict):
        tid = trace.get("traceId") or trace.get("trace_id") or trace.get("id")
        if isinstance(tid, str) and tid.strip():
            return tid.strip()
    return None


def _trace_id_from_headers(headers: Any) -> str | None:
    if headers is None:
        return None
    # FastAPI/Starlette headers act like a mapping.
    for key in ("x-ka2a-trace-id", "x-trace-id"):
        try:
            raw = headers.get(key)
        except Exception:
            raw = None
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def ensure_trace_metadata(metadata: dict[str, Any] | None, *, headers: Any = None) -> dict[str, Any] | None:
    """
    Ensure `urn:ka2a:trace` exists in metadata when tracing is enabled.

    If a trace id is already present, it is preserved.
    """

    if not tracing_enabled():
        return metadata
    out = dict(metadata or {})
    if trace_id_from_metadata(out):
        return out
    tid = _trace_id_from_headers(headers) or new_trace_id()
    out[KA2A_TRACE_METADATA_KEY] = {"traceId": tid}
    return out

