from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from threading import RLock
from typing import Any

from .models import (
    AgentControlPlaneState,
    AgentInstructionPreset,
    AgentSkill,
    AgentTemplate,
    AgentTemplateSkillBinding,
    AgentTemplateToolBinding,
    AgentTool,
    ModelVersionOption,
    ToolServer,
    WorkspaceAgent,
    WorkspaceAgentSkillBinding,
    WorkspaceAgentToolBinding,
    WorkspaceAiSettings,
    WorkspaceToolConnection,
)


logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _EntitySpec:
    field_name: str
    table_name: str
    model: type[Any]
    scalar_columns: tuple[str, ...]
    order_columns: tuple[str, ...] = ("created_at", "id")


class DatabaseAgentControlPlaneStore:
    def __init__(self, database_url: str) -> None:
        self._database_url = self._normalize_database_url(database_url)
        self._engine = None
        self._tables: dict[str, Any] = {}
        self._metadata = None
        self._lock = RLock()

    @staticmethod
    def _normalize_database_url(value: str) -> str:
        normalized = value.strip()
        if normalized.startswith("postgresql://"):
            return normalized.replace("postgresql://", "postgresql+psycopg://", 1)
        return normalized

    @staticmethod
    def _entity_specs() -> tuple[_EntitySpec, ...]:
        return (
            _EntitySpec(
                field_name="model_versions",
                table_name="a2a_model_versions",
                model=ModelVersionOption,
                scalar_columns=("provider", "provider_label", "model_name", "base_url"),
                order_columns=("provider", "model_name", "id"),
            ),
            _EntitySpec(
                field_name="tool_servers",
                table_name="a2a_tool_servers",
                model=ToolServer,
                scalar_columns=("scope", "profile", "is_active", "server_id", "name", "transport", "server_url"),
            ),
            _EntitySpec(
                field_name="tools",
                table_name="a2a_tools",
                model=AgentTool,
                scalar_columns=("scope", "profile", "is_active", "key", "display_name", "tool_server_id", "remote_tool_name"),
            ),
            _EntitySpec(
                field_name="skills",
                table_name="a2a_skills",
                model=AgentSkill,
                scalar_columns=("scope", "profile", "is_active", "key", "name"),
            ),
            _EntitySpec(
                field_name="instruction_presets",
                table_name="a2a_instruction_presets",
                model=AgentInstructionPreset,
                scalar_columns=("scope", "profile", "is_active", "key", "title", "instruction_type", "is_default"),
            ),
            _EntitySpec(
                field_name="templates",
                table_name="a2a_templates",
                model=AgentTemplate,
                scalar_columns=("slug", "name", "is_active", "is_featured", "allow_workspace_installs", "sort_order", "preferred_transport"),
                order_columns=("sort_order", "name", "id"),
            ),
            _EntitySpec(
                field_name="template_skill_bindings",
                table_name="a2a_template_skill_bindings",
                model=AgentTemplateSkillBinding,
                scalar_columns=("template_id", "skill_id", "order", "is_primary"),
                order_columns=("template_id", "order", "id"),
            ),
            _EntitySpec(
                field_name="template_tool_bindings",
                table_name="a2a_template_tool_bindings",
                model=AgentTemplateToolBinding,
                scalar_columns=("template_id", "tool_id", "order", "is_required"),
                order_columns=("template_id", "order", "id"),
            ),
            _EntitySpec(
                field_name="workspace_ai_settings",
                table_name="a2a_workspace_ai_settings",
                model=WorkspaceAiSettings,
                scalar_columns=("profile", "name", "version", "base_url"),
                order_columns=("profile", "id"),
            ),
            _EntitySpec(
                field_name="workspace_tool_connections",
                table_name="a2a_workspace_tool_connections",
                model=WorkspaceToolConnection,
                scalar_columns=(
                    "profile",
                    "tool_server_id",
                    "slug",
                    "name",
                    "connection_scope",
                    "owner_user",
                    "auth_type",
                    "status",
                ),
                order_columns=("profile", "name", "id"),
            ),
            _EntitySpec(
                field_name="workspace_agents",
                table_name="a2a_workspace_agents",
                model=WorkspaceAgent,
                scalar_columns=(
                    "profile",
                    "slug",
                    "name",
                    "source_template_id",
                    "origin",
                    "visibility",
                    "routing_policy",
                    "is_enabled",
                    "created_by",
                    "updated_by",
                    "preferred_transport",
                ),
                order_columns=("profile", "name", "id"),
            ),
            _EntitySpec(
                field_name="workspace_skill_bindings",
                table_name="a2a_workspace_skill_bindings",
                model=WorkspaceAgentSkillBinding,
                scalar_columns=("agent_id", "skill_id", "order", "is_primary"),
                order_columns=("agent_id", "order", "id"),
            ),
            _EntitySpec(
                field_name="workspace_tool_bindings",
                table_name="a2a_workspace_tool_bindings",
                model=WorkspaceAgentToolBinding,
                scalar_columns=("agent_id", "tool_id", "order", "is_required"),
                order_columns=("agent_id", "order", "id"),
            ),
        )

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        return None

    @staticmethod
    def _json_type(sa: Any) -> Any:
        json_type = sa.JSON()
        try:
            from sqlalchemy.dialects.postgresql import JSONB

            json_type = json_type.with_variant(JSONB, "postgresql")
        except Exception:
            pass
        return json_type

    def _build_table(self, sa: Any, metadata: Any, spec: _EntitySpec) -> Any:
        columns = [
            sa.Column("id", sa.String(length=80), primary_key=True),
            sa.Column("payload", self._json_type(sa), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        ]
        for name in spec.scalar_columns:
            if name in {"is_active", "is_featured", "allow_workspace_installs", "is_default", "is_enabled", "is_primary", "is_required"}:
                columns.append(sa.Column(name, sa.Boolean(), nullable=True))
            elif name == "sort_order" or name == "order":
                columns.append(sa.Column(name, sa.Integer(), nullable=True))
            else:
                columns.append(sa.Column(name, sa.String(length=255), nullable=True))

        table_args: list[Any] = []
        if spec.table_name == "a2a_workspace_ai_settings":
            table_args.append(sa.UniqueConstraint("profile", name="uq_a2a_workspace_ai_settings_profile"))
        if spec.table_name == "a2a_templates":
            table_args.append(sa.UniqueConstraint("slug", name="uq_a2a_templates_slug"))
        if spec.table_name == "a2a_workspace_agents":
            table_args.append(sa.UniqueConstraint("profile", "slug", name="uq_a2a_workspace_agents_profile_slug"))
        if spec.table_name == "a2a_model_versions":
            table_args.append(sa.UniqueConstraint("provider", "model_name", name="uq_a2a_model_versions_provider_model"))

        return sa.Table(spec.table_name, metadata, *columns, *table_args)

    @staticmethod
    def _control_plane_tables_exist(*, sa: Any, engine: Any, table_names: tuple[str, ...]) -> bool:
        """Avoid per-table schema reflection when the Postgres control plane is ready."""
        if engine.dialect.name != "postgresql":
            return False

        query = sa.text(
            "SELECT tablename FROM pg_catalog.pg_tables "
            "WHERE schemaname = current_schema() AND tablename IN :table_names"
        ).bindparams(sa.bindparam("table_names", expanding=True))
        with engine.connect() as conn:
            existing = {str(row[0]) for row in conn.execute(query, {"table_names": table_names})}
        return set(table_names).issubset(existing)

    def _ensure_runtime(self) -> None:
        if self._engine is not None and self._tables and self._metadata is not None:
            return
        try:
            import sqlalchemy as sa
        except Exception as exc:  # pragma: no cover
            raise RuntimeError(
                "SQLAlchemy is required for DATABASE_URL-backed A2A control-plane storage."
            ) from exc

        metadata = sa.MetaData()
        tables: dict[str, Any] = {}
        for spec in self._entity_specs():
            tables[spec.field_name] = self._build_table(sa, metadata, spec)

        tables["legacy_snapshot"] = sa.Table(
            "a2a_control_plane_state",
            metadata,
            sa.Column("namespace", sa.String(length=100), primary_key=True),
            sa.Column("payload", self._json_type(sa), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            extend_existing=True,
        )
        pool_recycle_s = max(30, int(os.getenv("KA2A_DB_POOL_RECYCLE_S") or "300"))
        engine_options: dict[str, Any] = {
            "future": True,
            "pool_pre_ping": True,
            "pool_recycle": pool_recycle_s,
            "pool_use_lifo": True,
        }
        if self._database_url.startswith(("postgresql", "postgres")):
            # The control plane persists JSON payloads. Avoid an hstore
            # catalog lookup that is particularly slow through PgBouncer.
            engine_options["use_native_hstore"] = False
        engine = sa.create_engine(self._database_url, **engine_options)
        table_names = tuple(table.name for table in metadata.tables.values())
        if not self._control_plane_tables_exist(sa=sa, engine=engine, table_names=table_names):
            metadata.create_all(engine)
        self._engine = engine
        self._metadata = metadata
        self._tables = tables

    def _reset_runtime(self) -> None:
        engine = self._engine
        self._engine = None
        self._tables = {}
        self._metadata = None
        if engine is not None:
            try:
                engine.dispose()
            except Exception:
                logger.debug("failed to dispose control-plane database engine", exc_info=True)

    @staticmethod
    def _is_retryable_db_exception(exc: Exception) -> bool:
        try:
            import sqlalchemy as sa
        except Exception:
            sa = None

        if sa is not None and isinstance(exc, sa.exc.DBAPIError):
            if getattr(exc, "connection_invalidated", False):
                return True

        message = str(exc).strip().lower()
        return any(
            phrase in message
            for phrase in (
                "consuming input failed",
                "unexpected eof while reading",
                "server closed the connection unexpectedly",
                "connection is closed",
                "broken pipe",
                "connection reset by peer",
            )
        )

    def _run_with_retry(self, operation):
        for attempt in range(1, 3):
            try:
                return operation()
            except Exception as exc:
                if attempt >= 2 or not self._is_retryable_db_exception(exc):
                    raise
                logger.warning(
                    "control-plane database operation failed; resetting engine and retrying",
                    extra={"attempt": attempt},
                    exc_info=True,
                )
                self._reset_runtime()

    def _spec_by_field_name(self, field_name: str) -> _EntitySpec:
        for spec in self._entity_specs():
            if spec.field_name == field_name:
                return spec
        raise KeyError(field_name)

    @staticmethod
    def _state_has_data(state: AgentControlPlaneState) -> bool:
        return any(
            getattr(state, field_name)
            for field_name in (
                "model_versions",
                "tool_servers",
                "tools",
                "skills",
                "instruction_presets",
                "templates",
                "template_skill_bindings",
                "template_tool_bindings",
                "workspace_ai_settings",
                "workspace_agents",
                "workspace_skill_bindings",
                "workspace_tool_bindings",
            )
        )

    def _entity_rows(self, state: AgentControlPlaneState, spec: _EntitySpec) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for item in getattr(state, spec.field_name):
            payload = item.model_dump(mode="json")
            row: dict[str, Any] = {
                "id": str(payload.get("id") or ""),
                "payload": payload,
                "created_at": self._parse_datetime(payload.get("created_at")),
                "updated_at": self._parse_datetime(payload.get("updated_at")),
            }
            for name in spec.scalar_columns:
                row[name] = payload.get(name)
            rows.append(row)
        return rows

    def _load_entity_list(self, conn: Any, spec: _EntitySpec) -> list[Any]:
        import sqlalchemy as sa

        table = self._tables[spec.field_name]
        order_columns = [table.c[name] for name in spec.order_columns if hasattr(table.c, name)]
        query = sa.select(table.c.payload)
        if order_columns:
            query = query.order_by(*order_columns)
        rows = conn.execute(query).all()
        return [spec.model.model_validate(row[0] or {}) for row in rows]

    def _build_filtered_query(
        self,
        spec: _EntitySpec,
        *,
        ids: list[str] | None = None,
        filters: dict[str, object] | None = None,
    ) -> Any:
        import sqlalchemy as sa

        table = self._tables[spec.field_name]
        query = sa.select(table.c.payload)
        if ids is not None:
            if not ids:
                return None
            query = query.where(table.c.id.in_(ids))
        for key, value in (filters or {}).items():
            if not hasattr(table.c, key):
                continue
            column = getattr(table.c, key)
            if isinstance(value, (list, tuple, set, frozenset)):
                values = list(value)
                if not values:
                    return None
                query = query.where(column.in_(values))
            else:
                query = query.where(column == value)
        order_columns = [table.c[name] for name in spec.order_columns if hasattr(table.c, name)]
        if order_columns:
            query = query.order_by(*order_columns)
        return query

    def _load_relational_state(self, conn: Any) -> AgentControlPlaneState:
        import sqlalchemy as sa

        specs = self._entity_specs()
        payload: dict[str, list[Any]] = {spec.field_name: [] for spec in specs}
        specs_by_field_name = {spec.field_name: spec for spec in specs}
        selects = []
        for spec in specs:
            table = self._tables[spec.field_name]
            selects.append(
                sa.select(
                    sa.literal(spec.field_name).label("field_name"),
                    table.c.payload.label("payload"),
                    table.c.created_at.label("created_at"),
                    table.c.id.label("id"),
                )
            )

        snapshot = sa.union_all(*selects).subquery("control_plane_snapshot")
        rows = conn.execute(
            sa.select(snapshot.c.field_name, snapshot.c.payload).order_by(
                snapshot.c.field_name,
                snapshot.c.created_at,
                snapshot.c.id,
            )
        ).all()
        for field_name, record_payload in rows:
            spec = specs_by_field_name[str(field_name)]
            payload[spec.field_name].append(spec.model.model_validate(record_payload or {}))
        return AgentControlPlaneState.model_validate(payload)

    def _load_legacy_snapshot(self, conn: Any) -> AgentControlPlaneState:
        import sqlalchemy as sa

        table = self._tables["legacy_snapshot"]
        row = conn.execute(sa.select(table.c.payload).where(table.c.namespace == "default")).first()
        if row is None:
            return AgentControlPlaneState()
        return AgentControlPlaneState.model_validate(row[0] or {})

    @staticmethod
    def _prefer_legacy_state(relational: AgentControlPlaneState, legacy: AgentControlPlaneState) -> bool:
        tracked_fields = (
            "model_versions",
            "tool_servers",
            "tools",
            "skills",
            "instruction_presets",
            "templates",
            "template_skill_bindings",
            "template_tool_bindings",
            "workspace_ai_settings",
            "workspace_agents",
            "workspace_skill_bindings",
            "workspace_tool_bindings",
        )
        if relational.model_dump(mode="json") == legacy.model_dump(mode="json"):
            return False
        for field_name in tracked_fields:
            if len(getattr(legacy, field_name)) > len(getattr(relational, field_name)):
                return True
        # Explicitly prefer the legacy snapshot when it still carries workspace/runtime state
        # that a pre-seeded relational catalog may not have imported yet.
        if legacy.workspace_ai_settings and not relational.workspace_ai_settings:
            return True
        if legacy.workspace_agents and not relational.workspace_agents:
            return True
        return False

    def _replace_all(self, conn: Any, state: AgentControlPlaneState) -> None:
        import sqlalchemy as sa

        # Delete bindings and workspace rows first, then catalog rows.
        delete_order = (
            "workspace_skill_bindings",
            "workspace_tool_bindings",
            "workspace_agents",
            "workspace_ai_settings",
            "template_skill_bindings",
            "template_tool_bindings",
            "templates",
            "instruction_presets",
            "skills",
            "tools",
            "tool_servers",
            "model_versions",
        )
        for field_name in delete_order:
            conn.execute(sa.delete(self._tables[field_name]))

        for spec in self._entity_specs():
            rows = self._entity_rows(state, spec)
            if rows:
                conn.execute(self._tables[spec.field_name].insert(), rows)

        conn.execute(sa.delete(self._tables["legacy_snapshot"]).where(self._tables["legacy_snapshot"].c.namespace == "default"))

    def list_records(
        self,
        field_name: str,
        *,
        ids: list[str] | None = None,
        filters: dict[str, object] | None = None,
    ) -> list[object]:
        with self._lock:
            def _operation() -> list[object]:
                self._ensure_runtime()
                spec = self._spec_by_field_name(field_name)
                query = self._build_filtered_query(spec, ids=ids, filters=filters)
                if query is None:
                    return []
                assert self._engine is not None
                with self._engine.begin() as conn:
                    rows = conn.execute(query).all()
                return [spec.model.model_validate(row[0] or {}) for row in rows]

            return self._run_with_retry(_operation)

    def get_record(
        self,
        field_name: str,
        *,
        record_id: str | None = None,
        filters: dict[str, object] | None = None,
    ) -> object | None:
        records = self.list_records(field_name, ids=[record_id] if record_id is not None else None, filters=filters)
        return records[0] if records else None

    def _row_for_record(self, spec: _EntitySpec, record: object) -> dict[str, Any]:
        payload = record.model_dump(mode="json")
        row: dict[str, Any] = {
            "id": str(payload.get("id") or ""),
            "payload": payload,
            "created_at": self._parse_datetime(payload.get("created_at")),
            "updated_at": self._parse_datetime(payload.get("updated_at")),
        }
        for name in spec.scalar_columns:
            row[name] = payload.get(name)
        return row

    def upsert_record(self, field_name: str, record: object) -> object:
        with self._lock:
            def _operation() -> object:
                self._ensure_runtime()
                import sqlalchemy as sa

                spec = self._spec_by_field_name(field_name)
                table = self._tables[field_name]
                row = self._row_for_record(spec, record)
                assert self._engine is not None
                with self._engine.begin() as conn:
                    updated = conn.execute(
                        sa.update(table).where(table.c.id == row["id"]).values(**row)
                    )
                    if not updated.rowcount:
                        conn.execute(sa.insert(table).values(**row))
                return record

            self._run_with_retry(_operation)
            return record

    def upsert_records(self, field_name: str, records: list[object]) -> list[object]:
        if not records:
            return []
        with self._lock:
            def _operation() -> list[object]:
                self._ensure_runtime()
                import sqlalchemy as sa

                spec = self._spec_by_field_name(field_name)
                table = self._tables[field_name]
                rows = [self._row_for_record(spec, record) for record in records]
                assert self._engine is not None
                with self._engine.begin() as conn:
                    ids = [row["id"] for row in rows]
                    conn.execute(sa.delete(table).where(table.c.id.in_(ids)))
                    conn.execute(sa.insert(table), rows)
                return records

            return self._run_with_retry(_operation)

    def delete_records(
        self,
        field_name: str,
        *,
        ids: list[str] | None = None,
        filters: dict[str, object] | None = None,
    ) -> int:
        with self._lock:
            def _operation() -> int:
                self._ensure_runtime()
                import sqlalchemy as sa

                self._spec_by_field_name(field_name)
                table = self._tables[field_name]
                query = sa.delete(table)
                if ids is not None:
                    if not ids:
                        return 0
                    query = query.where(table.c.id.in_(ids))
                for key, value in (filters or {}).items():
                    if not hasattr(table.c, key):
                        continue
                    column = getattr(table.c, key)
                    if isinstance(value, (list, tuple, set, frozenset)):
                        values = list(value)
                        if not values:
                            return 0
                        query = query.where(column.in_(values))
                    else:
                        query = query.where(column == value)
                assert self._engine is not None
                with self._engine.begin() as conn:
                    result = conn.execute(query)
                return int(result.rowcount or 0)

            return self._run_with_retry(_operation)

    def load(self) -> AgentControlPlaneState:
        with self._lock:
            def _operation() -> AgentControlPlaneState:
                self._ensure_runtime()
                import sqlalchemy as sa

                assert self._engine is not None
                with self._engine.begin() as conn:
                    state = self._load_relational_state(conn)
                    legacy_state = self._load_legacy_snapshot(conn)
                    if self._state_has_data(legacy_state) and (
                        not self._state_has_data(state) or self._prefer_legacy_state(state, legacy_state)
                    ):
                        self._replace_all(conn, legacy_state)
                        return legacy_state
                    if self._state_has_data(legacy_state):
                        conn.execute(
                            sa.delete(self._tables["legacy_snapshot"]).where(
                                self._tables["legacy_snapshot"].c.namespace == "default"
                            )
                        )
                    if self._state_has_data(state):
                        return state
                return AgentControlPlaneState()

            return self._run_with_retry(_operation)

    def save(self, state: AgentControlPlaneState) -> AgentControlPlaneState:
        with self._lock:
            def _operation() -> AgentControlPlaneState:
                self._ensure_runtime()
                assert self._engine is not None
                with self._engine.begin() as conn:
                    self._replace_all(conn, state)
                return state

            return self._run_with_retry(_operation)
            return state
