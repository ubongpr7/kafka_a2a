from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

from kafka_a2a.decisioning.config import DecisioningConfig
from kafka_a2a.decisioning.contracts import (
    DecisionErrorCategory,
    DecisionFallbackReason,
    DecisionRequest,
    DecisionResult,
    DecisionUsage,
)


def _require_httpx() -> Any:
    try:
        import httpx  # type: ignore
    except Exception as exc:  # pragma: no cover - exercised by configuration fallback
        raise RuntimeError("Decisioning requires the `decisioning` extra (e.g. `uv sync --extra decisioning`).") from exc
    return httpx


class TypeSafeDecisionProvider:
    """Adapter for TypeSafe's System One API; vendor shapes stop at this boundary."""

    provider_name = "typesafe"

    def __init__(
        self,
        *,
        config: DecisioningConfig,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._cfg = config
        self._client_factory = client_factory
        self._client: Any | None = None

    def _client_or_create(self) -> Any:
        if self._client is not None:
            return self._client
        if self._client_factory is not None:
            self._client = self._client_factory()
            return self._client
        httpx = _require_httpx()
        self._client = httpx.AsyncClient(timeout=self._cfg.timeout_s)
        return self._client

    async def decide(self, request: DecisionRequest) -> DecisionResult:
        if not self._cfg.api_key or not self._cfg.model:
            return DecisionResult.fallback(
                provider=self.provider_name,
                model=self._cfg.model,
                fallback_reason=DecisionFallbackReason.missing_configuration,
                error_category=DecisionErrorCategory.unavailable,
            )
        payload = {
            "state": dict(request.state),
            "model": self._cfg.model,
            "questions": {
                request.question.key: {
                    "type": "choice",
                    "instructions": request.question.instructions,
                    "criteria": dict(request.question.candidates),
                }
            },
        }
        try:
            response = await self._client_or_create().post(
                self._cfg.endpoint,
                headers={"Authorization": f"Bearer {self._cfg.api_key}", "Content-Type": "application/json"},
                json=payload,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return self._failure(DecisionErrorCategory.timeout)
        except Exception as exc:
            httpx = _require_httpx()
            if isinstance(exc, httpx.TimeoutException):
                return self._failure(DecisionErrorCategory.timeout)
            if isinstance(exc, httpx.NetworkError):
                return self._failure(DecisionErrorCategory.network)
            return self._failure(DecisionErrorCategory.network)

        status_code = int(getattr(response, "status_code", 0))
        if status_code == 429:
            return self._failure(DecisionErrorCategory.rate_limited)
        if 500 <= status_code <= 599:
            return self._failure(DecisionErrorCategory.provider_5xx)
        if status_code < 200 or status_code >= 300:
            return self._failure(DecisionErrorCategory.provider_4xx)
        try:
            payload_obj = response.json()
        except Exception:
            return self._failure(DecisionErrorCategory.invalid_response)
        if not isinstance(payload_obj, dict):
            return self._failure(DecisionErrorCategory.invalid_response)

        answers = payload_obj.get("answers")
        answer = answers.get(request.question.key) if isinstance(answers, dict) else None
        if not isinstance(answer, dict) or str(answer.get("type") or "").lower() != "choice":
            return self._failure(DecisionErrorCategory.invalid_response)
        selected = answer.get("choice")
        if not isinstance(selected, str) or selected not in request.question.candidates:
            return self._failure(DecisionErrorCategory.unexpected_candidate)
        probabilities = self._parse_probabilities(answer.get("probabilities"), set(request.question.candidates))
        if probabilities is None:
            return self._failure(DecisionErrorCategory.malformed_probabilities)
        confidence = answer.get("confidence")
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= float(confidence) <= 1:
            return self._failure(DecisionErrorCategory.missing_confidence)
        return DecisionResult(
            provider=self.provider_name,
            model=str(payload_obj.get("model") or self._cfg.model),
            selected_candidate=selected,
            probabilities=probabilities,
            confidence=float(confidence),
            usage=self._parse_usage(payload_obj.get("usage")),
        )

    def _failure(self, category: DecisionErrorCategory) -> DecisionResult:
        return DecisionResult.fallback(
            provider=self.provider_name,
            model=self._cfg.model,
            fallback_reason=DecisionFallbackReason.provider_failure,
            error_category=category,
        )

    @staticmethod
    def _parse_probabilities(value: Any, candidates: set[str]) -> dict[str, float] | None:
        if not isinstance(value, dict) or set(value) != candidates:
            return None
        parsed: dict[str, float] = {}
        for key, probability in value.items():
            if not isinstance(probability, (int, float)) or isinstance(probability, bool):
                return None
            numeric = float(probability)
            if not 0 <= numeric <= 1:
                return None
            parsed[str(key)] = numeric
        if not 0.98 <= sum(parsed.values()) <= 1.02:
            return None
        return parsed

    @staticmethod
    def _parse_usage(value: Any) -> DecisionUsage | None:
        if not isinstance(value, dict):
            return None
        def _non_negative_int(raw: Any) -> int | None:
            if isinstance(raw, bool):
                return None
            if isinstance(raw, (int, float)) and raw >= 0:
                return int(raw)
            return None

        input_tokens = _non_negative_int(value.get("input_tokens"))
        output_tokens = _non_negative_int(value.get("output_tokens"))
        if input_tokens is None and output_tokens is None:
            return None
        return DecisionUsage(input_tokens=input_tokens, output_tokens=output_tokens)

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        close = getattr(client, "aclose", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result
