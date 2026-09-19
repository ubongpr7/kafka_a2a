from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Mapping

from kafka_a2a.decisioning.contracts import DecisionCapability, DecisionMode

DEFAULT_TYPESAFE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
VOICE_DECISION_CAPABILITIES = frozenset(
    {
        DecisionCapability.voice_turn_readiness,
        DecisionCapability.voice_turn_relation,
        DecisionCapability.voice_response_speakability,
    }
)


def _parse_bool(value: str | None, *, default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _parse_positive_float(value: str | None, *, default: float, name: str) -> float:
    if value is None or not value.strip():
        return default
    try:
        parsed = float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


def _parse_positive_int(value: str | None, *, default: int, name: str) -> int:
    if value is None or not value.strip():
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be greater than zero")
    return parsed


def _parse_shadow_sample_rate(value: str | None, *, default: float, name: str) -> float:
    try:
        parsed = default if value is None or not value.strip() else float(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number from 0 to 1") from exc
    if not 0 <= parsed <= 1:
        raise ValueError(f"{name} must be between 0 and 1")
    return parsed


def _parse_confidence(value: str | None, *, default: float, name: str) -> float:
    return _parse_shadow_sample_rate(value, default=default, name=name)


@dataclass(frozen=True, slots=True)
class DecisioningConfig:
    """Platform configuration only. Tenant credentials never appear here."""

    enabled: bool = False
    provider: str = "typesafe"
    mode: DecisionMode = DecisionMode.off
    api_key_env: str = "TYPESAFE_API_KEY"
    api_key: str | None = field(default=None, repr=False)
    endpoint: str = DEFAULT_TYPESAFE_ENDPOINT
    model: str | None = None
    timeout_s: float = 0.8
    max_concurrency: int = 4
    retry_max_retries: int = 1
    circuit_failure_threshold: int = 5
    circuit_open_s: float = 30.0
    state_max_chars: int = 6000
    history_items: int = 4
    shadow_sample_rate: float = 0.1
    queue_maxsize: int = 200
    telemetry_topic: str = "ka2a.decision.events.v1"
    policy_version: str = "jev-decisioning-v1"
    state_hash_key_env: str = "KA2A_DECISIONING_STATE_HASH_KEY"
    state_hash_key: str | None = field(default=None, repr=False)
    enforce_capabilities: frozenset[DecisionCapability] = field(default_factory=frozenset)
    enforce_min_confidence: float = 0.85
    max_candidates: int = 20
    response_retry_enabled: bool = False
    voice_enabled: bool = False
    voice_timeout_s: float = 0.25
    voice_shadow_sample_rate: float = 0.1
    voice_enforce_capabilities: frozenset[DecisionCapability] = field(default_factory=frozenset)
    voice_enforce_min_confidence: float = 0.92
    voice_drain_timeout_s: float = 0.25

    @property
    def is_active(self) -> bool:
        return (
            self.enabled
            and self.mode in {DecisionMode.shadow, DecisionMode.enforce}
            and self.provider == "typesafe"
            and bool(self.api_key)
            and bool(self.model)
        )

    @property
    def is_shadow_active(self) -> bool:
        return self.is_active

    def can_enforce(self, capability: DecisionCapability) -> bool:
        return self.is_active and self.mode is DecisionMode.enforce and capability in self.enforce_capabilities

    def for_voice(self) -> "DecisioningConfig":
        """Return a LiveKit-safe view with an independent timeout and allowlist."""

        if not self.voice_enabled:
            return replace(
                self,
                enabled=False,
                mode=DecisionMode.off,
                enforce_capabilities=frozenset(),
            )
        return replace(
            self,
            timeout_s=self.voice_timeout_s,
            retry_max_retries=0,
            shadow_sample_rate=self.voice_shadow_sample_rate,
            enforce_capabilities=self.voice_enforce_capabilities,
            enforce_min_confidence=self.voice_enforce_min_confidence,
        )

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "DecisioningConfig":
        env_map = env or os.environ
        mode_value = (env_map.get("KA2A_DECISIONING_MODE") or "off").strip().lower()
        try:
            mode = DecisionMode(mode_value)
        except ValueError as exc:
            raise ValueError("KA2A_DECISIONING_MODE must be off, shadow, or enforce") from exc

        sample_rate = _parse_shadow_sample_rate(
            env_map.get("KA2A_DECISIONING_SHADOW_SAMPLE_RATE"),
            default=0.1,
            name="KA2A_DECISIONING_SHADOW_SAMPLE_RATE",
        )
        enforce_min_confidence = _parse_confidence(
            env_map.get("KA2A_DECISIONING_ENFORCE_MIN_CONFIDENCE"),
            default=0.85,
            name="KA2A_DECISIONING_ENFORCE_MIN_CONFIDENCE",
        )

        known_capabilities = {capability.value: capability for capability in DecisionCapability}
        configured_capabilities = {
            item.strip().lower()
            for item in (env_map.get("KA2A_DECISIONING_ENFORCE_CAPABILITIES") or "").split(",")
            if item.strip()
        }
        unknown_capabilities = configured_capabilities.difference(known_capabilities)
        if unknown_capabilities:
            raise ValueError(
                "KA2A_DECISIONING_ENFORCE_CAPABILITIES contains unsupported capabilities: "
                + ", ".join(sorted(unknown_capabilities))
            )

        configured_voice_capabilities = {
            item.strip().lower()
            for item in (env_map.get("KA2A_VOICE_DECISIONING_ENFORCE_CAPABILITIES") or "").split(",")
            if item.strip()
        }
        unknown_voice_capabilities = configured_voice_capabilities.difference(known_capabilities)
        if unknown_voice_capabilities:
            raise ValueError(
                "KA2A_VOICE_DECISIONING_ENFORCE_CAPABILITIES contains unsupported capabilities: "
                + ", ".join(sorted(unknown_voice_capabilities))
            )
        voice_enforce_capabilities = frozenset(known_capabilities[item] for item in configured_voice_capabilities)
        invalid_voice_capabilities = voice_enforce_capabilities.difference(VOICE_DECISION_CAPABILITIES)
        if invalid_voice_capabilities:
            raise ValueError(
                "KA2A_VOICE_DECISIONING_ENFORCE_CAPABILITIES supports only voice capabilities: "
                + ", ".join(sorted(capability.value for capability in invalid_voice_capabilities))
            )

        api_key_env = (env_map.get("KA2A_DECISIONING_API_KEY_ENV") or "TYPESAFE_API_KEY").strip()
        hash_key_env = (env_map.get("KA2A_DECISIONING_STATE_HASH_KEY_ENV") or "KA2A_DECISIONING_STATE_HASH_KEY").strip()
        if not api_key_env or not hash_key_env:
            raise ValueError("Decisioning secret environment-variable names must not be empty")

        return cls(
            enabled=_parse_bool(env_map.get("KA2A_DECISIONING_ENABLED"), default=False),
            provider=(env_map.get("KA2A_DECISIONING_PROVIDER") or "typesafe").strip().lower(),
            mode=mode,
            api_key_env=api_key_env,
            api_key=(env_map.get(api_key_env) or "").strip() or None,
            endpoint=(env_map.get("KA2A_DECISIONING_ENDPOINT") or DEFAULT_TYPESAFE_ENDPOINT).strip(),
            model=(env_map.get("KA2A_DECISIONING_MODEL") or "").strip() or None,
            timeout_s=_parse_positive_float(
                env_map.get("KA2A_DECISIONING_TIMEOUT_S"), default=0.8, name="KA2A_DECISIONING_TIMEOUT_S"
            ),
            max_concurrency=_parse_positive_int(
                env_map.get("KA2A_DECISIONING_MAX_CONCURRENCY"), default=4, name="KA2A_DECISIONING_MAX_CONCURRENCY"
            ),
            retry_max_retries=max(
                0,
                int(env_map.get("KA2A_DECISIONING_RETRY_MAX_RETRIES") or "1"),
            ),
            circuit_failure_threshold=_parse_positive_int(
                env_map.get("KA2A_DECISIONING_CIRCUIT_FAILURE_THRESHOLD"),
                default=5,
                name="KA2A_DECISIONING_CIRCUIT_FAILURE_THRESHOLD",
            ),
            circuit_open_s=_parse_positive_float(
                env_map.get("KA2A_DECISIONING_CIRCUIT_OPEN_S"), default=30.0, name="KA2A_DECISIONING_CIRCUIT_OPEN_S"
            ),
            state_max_chars=_parse_positive_int(
                env_map.get("KA2A_DECISIONING_STATE_MAX_CHARS"), default=6000, name="KA2A_DECISIONING_STATE_MAX_CHARS"
            ),
            history_items=max(
                0,
                int(env_map.get("KA2A_DECISIONING_HISTORY_ITEMS") or "4"),
            ),
            shadow_sample_rate=sample_rate,
            queue_maxsize=_parse_positive_int(
                env_map.get("KA2A_DECISIONING_QUEUE_MAXSIZE"), default=200, name="KA2A_DECISIONING_QUEUE_MAXSIZE"
            ),
            telemetry_topic=(env_map.get("KA2A_DECISIONING_TELEMETRY_TOPIC") or "ka2a.decision.events.v1").strip(),
            policy_version=(env_map.get("KA2A_DECISIONING_POLICY_VERSION") or "jev-decisioning-v1").strip(),
            state_hash_key_env=hash_key_env,
            state_hash_key=(env_map.get(hash_key_env) or "").strip() or None,
            enforce_capabilities=frozenset(known_capabilities[item] for item in configured_capabilities),
            enforce_min_confidence=enforce_min_confidence,
            max_candidates=_parse_positive_int(
                env_map.get("KA2A_DECISIONING_MAX_CANDIDATES"), default=20, name="KA2A_DECISIONING_MAX_CANDIDATES"
            ),
            response_retry_enabled=_parse_bool(env_map.get("KA2A_DECISIONING_RESPONSE_RETRY_ENABLED"), default=False),
            voice_enabled=_parse_bool(env_map.get("KA2A_VOICE_DECISIONING_ENABLED"), default=False),
            voice_timeout_s=_parse_positive_float(
                env_map.get("KA2A_VOICE_DECISIONING_TIMEOUT_S"), default=0.25, name="KA2A_VOICE_DECISIONING_TIMEOUT_S"
            ),
            voice_shadow_sample_rate=_parse_shadow_sample_rate(
                env_map.get("KA2A_VOICE_DECISIONING_SHADOW_SAMPLE_RATE"),
                default=sample_rate,
                name="KA2A_VOICE_DECISIONING_SHADOW_SAMPLE_RATE",
            ),
            voice_enforce_capabilities=voice_enforce_capabilities,
            voice_enforce_min_confidence=_parse_confidence(
                env_map.get("KA2A_VOICE_DECISIONING_ENFORCE_MIN_CONFIDENCE"),
                default=0.92,
                name="KA2A_VOICE_DECISIONING_ENFORCE_MIN_CONFIDENCE",
            ),
            voice_drain_timeout_s=_parse_positive_float(
                env_map.get("KA2A_VOICE_DECISIONING_DRAIN_TIMEOUT_S"),
                default=0.25,
                name="KA2A_VOICE_DECISIONING_DRAIN_TIMEOUT_S",
            ),
        )

    @classmethod
    def disabled(cls) -> "DecisioningConfig":
        return cls()
