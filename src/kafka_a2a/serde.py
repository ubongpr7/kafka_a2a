from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, BaseModel):
        return value.model_dump(by_alias=True, exclude_none=True)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def dumps(value: Any) -> bytes:
    if isinstance(value, BaseModel):
        return value.model_dump_json(by_alias=True, exclude_none=True).encode("utf-8")
    return json.dumps(value, separators=(",", ":"), default=_json_default).encode("utf-8")


def loads(data: bytes | str) -> Any:
    if isinstance(data, bytes):
        data = data.decode("utf-8")
    return json.loads(data)

