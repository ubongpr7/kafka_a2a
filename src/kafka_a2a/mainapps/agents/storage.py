from __future__ import annotations

import json
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock

from kafka_a2a.core.config import A2AAppSettings

from .db import DatabaseAgentControlPlaneStore
from .models import AgentControlPlaneState


class JsonAgentControlPlaneStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = RLock()

    def load(self) -> AgentControlPlaneState:
        with self._lock:
            if not self._path.exists():
                return AgentControlPlaneState()
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            return AgentControlPlaneState.model_validate(payload)

    def save(self, state: AgentControlPlaneState) -> AgentControlPlaneState:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = state.model_dump(mode="json")
            with NamedTemporaryFile("w", encoding="utf-8", dir=self._path.parent, delete=False) as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=True)
                handle.flush()
                temp_path = Path(handle.name)
            temp_path.replace(self._path)
            return state

    def list_records(
        self,
        field_name: str,
        *,
        ids: list[str] | None = None,
        filters: dict[str, object] | None = None,
    ) -> list[object]:
        items = list(getattr(self.load(), field_name))
        if ids is not None:
            allowed = set(ids)
            items = [item for item in items if getattr(item, "id", None) in allowed]
        for key, value in (filters or {}).items():
            if isinstance(value, (list, tuple, set, frozenset)):
                allowed = set(value)
                items = [item for item in items if getattr(item, key, None) in allowed]
            else:
                items = [item for item in items if getattr(item, key, None) == value]
        return items

    def get_record(
        self,
        field_name: str,
        *,
        record_id: str | None = None,
        filters: dict[str, object] | None = None,
    ) -> object | None:
        ids = [record_id] if record_id is not None else None
        records = self.list_records(field_name, ids=ids, filters=filters)
        return records[0] if records else None

    def upsert_record(self, field_name: str, record: object) -> object:
        state = self.load()
        items = list(getattr(state, field_name))
        record_id = getattr(record, "id")
        replaced = False
        for index, item in enumerate(items):
            if getattr(item, "id", None) == record_id:
                items[index] = record
                replaced = True
                break
        if not replaced:
            items.append(record)
        setattr(state, field_name, items)
        self.save(state)
        return record

    def upsert_records(self, field_name: str, records: list[object]) -> list[object]:
        if not records:
            return []
        state = self.load()
        items = list(getattr(state, field_name))
        by_id = {getattr(item, "id", None): item for item in items}
        for record in records:
            by_id[getattr(record, "id")] = record
        setattr(state, field_name, list(by_id.values()))
        self.save(state)
        return records

    def delete_records(
        self,
        field_name: str,
        *,
        ids: list[str] | None = None,
        filters: dict[str, object] | None = None,
    ) -> int:
        state = self.load()
        items = list(getattr(state, field_name))
        before = len(items)
        allowed_ids = set(ids or [])

        def matches(item: object) -> bool:
            if ids is not None and getattr(item, "id", None) not in allowed_ids:
                return False
            for key, value in (filters or {}).items():
                candidate = getattr(item, key, None)
                if isinstance(value, (list, tuple, set, frozenset)):
                    if candidate not in set(value):
                        return False
                elif candidate != value:
                    return False
            return True

        kept = [item for item in items if not matches(item)]
        setattr(state, field_name, kept)
        self.save(state)
        return before - len(kept)


def build_agent_control_plane_store(settings: A2AAppSettings):
    if settings.database_url:
        return DatabaseAgentControlPlaneStore(settings.database_url)
    return JsonAgentControlPlaneStore(settings.control_plane_store_path)
