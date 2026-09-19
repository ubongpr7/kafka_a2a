from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass

from kafka_a2a.decisioning.config import DecisioningConfig
from kafka_a2a.decisioning.contracts import (
    DecisionErrorCategory,
    DecisionFallbackReason,
    DecisionProvider,
    DecisionRequest,
    DecisionResult,
)
from kafka_a2a.decisioning.telemetry import DecisionTelemetryEvent, DecisionTelemetrySink, StructuredLogDecisionTelemetry

logger = logging.getLogger("kafka_a2a.decisioning")

_TRANSIENT_ERRORS = {
    DecisionErrorCategory.timeout,
    DecisionErrorCategory.network,
    DecisionErrorCategory.rate_limited,
    DecisionErrorCategory.provider_5xx,
    DecisionErrorCategory.unavailable,
}


@dataclass(frozen=True, slots=True)
class ShadowDecisionObservation:
    request: DecisionRequest
    baseline_decision: str | None


class DecisionService:
    """Bounded asynchronous dispatcher for fail-open shadow observations."""

    def __init__(
        self,
        *,
        config: DecisioningConfig,
        provider: DecisionProvider | None,
        telemetry: DecisionTelemetrySink | None = None,
    ) -> None:
        self._cfg = config
        self._provider = provider
        self._telemetry = telemetry or StructuredLogDecisionTelemetry()
        self._queue: asyncio.Queue[ShadowDecisionObservation] | None = None
        self._workers: set[asyncio.Task[None]] = set()
        self._closed = False
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0
        self._circuit_lock = asyncio.Lock()
        self._provider_semaphore = asyncio.Semaphore(max(1, config.max_concurrency))

    @property
    def is_shadow_active(self) -> bool:
        return self._cfg.is_shadow_active and self._provider is not None and not self._closed

    def should_sample(self, request: DecisionRequest) -> bool:
        rate = self._cfg.shadow_sample_rate
        if rate <= 0:
            return False
        if rate >= 1:
            return True
        attribution = request.attribution
        stable_key = attribution.trace_id or attribution.task_id or attribution.context_id
        if not stable_key:
            return False
        value = int.from_bytes(hashlib.sha256(stable_key.encode("utf-8")).digest()[:8], "big") / float(2**64)
        return value < rate

    def submit_shadow(self, observation: ShadowDecisionObservation) -> bool:
        if not self.is_shadow_active or not self.should_sample(observation.request):
            return False
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return False
        self._ensure_workers()
        assert self._queue is not None
        try:
            self._queue.put_nowait(observation)
        except asyncio.QueueFull:
            self._emit(
                observation=observation,
                result=DecisionResult.fallback(
                    provider=self._cfg.provider,
                    model=self._cfg.model,
                    fallback_reason=DecisionFallbackReason.queue_full,
                ),
                comparison="unknown",
                latency_ms=0.0,
                retry_count=0,
                outcome="dropped",
            )
            return False
        return True

    def _ensure_workers(self) -> None:
        if self._queue is None:
            self._queue = asyncio.Queue(maxsize=self._cfg.queue_maxsize)
        target = self._cfg.max_concurrency
        while len(self._workers) < target:
            worker = asyncio.create_task(self._worker(), name="ka2a-decisioning-shadow")
            self._workers.add(worker)
            worker.add_done_callback(self._workers.discard)

    async def _worker(self) -> None:
        assert self._queue is not None
        while True:
            observation = await self._queue.get()
            try:
                await self._execute(observation)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("decisioning shadow worker failed")
            finally:
                self._queue.task_done()

    async def _execute(self, observation: ShadowDecisionObservation) -> None:
        await self._evaluate(observation, outcome_on_success="success")

    async def decide_now(self, observation: ShadowDecisionObservation) -> DecisionResult:
        """Return a bounded decision for an explicitly enabled enforcement capability."""

        if not self.is_shadow_active:
            return DecisionResult.fallback(
                provider=self._cfg.provider,
                model=self._cfg.model,
                fallback_reason=DecisionFallbackReason.missing_configuration,
                error_category=DecisionErrorCategory.unavailable,
            )
        return await self._evaluate(observation, outcome_on_success="enforcement_evaluated")

    async def _evaluate(self, observation: ShadowDecisionObservation, *, outcome_on_success: str) -> DecisionResult:
        started = time.monotonic()
        result: DecisionResult
        retry_count = 0
        if await self._circuit_is_open():
            result = DecisionResult.fallback(
                provider=self._cfg.provider,
                model=self._cfg.model,
                fallback_reason=DecisionFallbackReason.circuit_open,
                error_category=DecisionErrorCategory.circuit_open,
            )
        else:
            result, retry_count = await self._call_with_retry(observation.request)
        await self._record_circuit_result(result)
        comparison = "unknown"
        if result.is_success and observation.baseline_decision:
            comparison = "matched" if result.selected_candidate == observation.baseline_decision else "diverged"
        if result.error_category is not None:
            outcome = "fallback"
        elif result.is_success:
            outcome = outcome_on_success
        else:
            outcome = "fallback"
        self._emit(
            observation=observation,
            result=result,
            comparison=comparison,
            latency_ms=(time.monotonic() - started) * 1000,
            retry_count=retry_count,
            outcome=outcome,
        )
        return result

    async def _call_with_retry(self, request: DecisionRequest) -> tuple[DecisionResult, int]:
        assert self._provider is not None
        attempts = 0
        while True:
            try:
                async with self._provider_semaphore:
                    result = await asyncio.wait_for(self._provider.decide(request), timeout=self._cfg.timeout_s)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                result = DecisionResult.fallback(
                    provider=self._cfg.provider,
                    model=self._cfg.model,
                    fallback_reason=DecisionFallbackReason.provider_failure,
                    error_category=DecisionErrorCategory.timeout,
                )
            except Exception:
                logger.warning("decisioning provider invocation failed; using baseline behavior")
                result = DecisionResult.fallback(
                    provider=self._cfg.provider,
                    model=self._cfg.model,
                    fallback_reason=DecisionFallbackReason.provider_failure,
                    error_category=DecisionErrorCategory.network,
                )
            if result.error_category not in _TRANSIENT_ERRORS or attempts >= self._cfg.retry_max_retries:
                return result, attempts
            attempts += 1

    async def _circuit_is_open(self) -> bool:
        async with self._circuit_lock:
            if self._circuit_open_until <= 0:
                return False
            if time.monotonic() < self._circuit_open_until:
                return True
            self._circuit_open_until = 0.0
            self._consecutive_failures = 0
            return False

    async def _record_circuit_result(self, result: DecisionResult) -> None:
        async with self._circuit_lock:
            if result.is_success:
                self._consecutive_failures = 0
                return
            if result.error_category not in _TRANSIENT_ERRORS:
                return
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._cfg.circuit_failure_threshold:
                self._circuit_open_until = time.monotonic() + self._cfg.circuit_open_s

    def _emit(
        self,
        *,
        observation: ShadowDecisionObservation,
        result: DecisionResult,
        comparison: str,
        latency_ms: float,
        retry_count: int,
        outcome: str,
    ) -> None:
        try:
            self._telemetry.emit(
                DecisionTelemetryEvent(
                    request=observation.request,
                    result=result,
                    baseline_decision=observation.baseline_decision,
                    comparison=comparison,
                    latency_ms=latency_ms,
                    retry_count=retry_count,
                    outcome=outcome,
                    telemetry_topic=self._cfg.telemetry_topic,
                )
            )
        except Exception:
            logger.warning("decisioning telemetry emission failed", exc_info=True)

    async def aclose(self, *, drain_timeout_s: float | None = None) -> None:
        if drain_timeout_s and drain_timeout_s > 0 and self._queue is not None and self._workers:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=drain_timeout_s)
            except asyncio.TimeoutError:
                logger.debug("decisioning shutdown drain timed out", extra={"timeout_s": drain_timeout_s})
        self._closed = True
        workers = tuple(self._workers)
        for worker in workers:
            worker.cancel()
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        self._workers.clear()
        if self._queue is not None:
            while not self._queue.empty():
                try:
                    self._queue.get_nowait()
                    self._queue.task_done()
                except asyncio.QueueEmpty:
                    break
        if self._provider is not None:
            await self._provider.aclose()
