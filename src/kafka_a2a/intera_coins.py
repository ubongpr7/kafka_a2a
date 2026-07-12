from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any
from urllib import error, request


logger = logging.getLogger(__name__)


def _parse_bool_env(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_int_env(name: str, *, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _parts_have_visible_content(parts: Any) -> bool:
    if not isinstance(parts, list):
        return False
    for item in parts:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind == "text" and str(item.get("text") or "").strip():
            return True
        if kind == "data":
            data = item.get("data")
            if data not in (None, "", [], {}):
                return True
    return False


def build_a2a_coin_reference(*, profile_id: str, task_id: str) -> str:
    return f"a2a:{profile_id}:{task_id}"


@dataclass(slots=True)
class PendingA2ACharge:
    profile_id: str
    task_id: str
    conversation_id: str | None = None
    prompt_text: str | None = None

    @property
    def reference_id(self) -> str:
        return build_a2a_coin_reference(profile_id=self.profile_id, task_id=self.task_id)


@dataclass(slots=True)
class A2ACompletionChargeTracker:
    profile_id: str | None
    conversation_id: str | None = None
    prompt_text: str | None = None
    current_task_id: str | None = None
    assistant_result_seen: bool = False
    charged_reference_ids: set[str] = field(default_factory=set)

    def evaluate(self, payload: dict[str, Any] | None) -> PendingA2ACharge | None:
        if not self.profile_id or not isinstance(payload, dict):
            return None

        kind = str(payload.get("kind") or "").strip()
        task_id = str(payload.get("taskId") or payload.get("id") or "").strip() or self.current_task_id
        if task_id:
            self.current_task_id = task_id

        if kind == "artifact-update":
            artifact = payload.get("artifact")
            if isinstance(artifact, dict) and str(artifact.get("name") or "").strip() == "result":
                self.assistant_result_seen = self.assistant_result_seen or _parts_have_visible_content(artifact.get("parts"))
            return None

        if kind != "status-update":
            return None

        status = payload.get("status")
        if not isinstance(status, dict):
            return None
        state = str(status.get("state") or "").strip().lower()
        if state != "completed" or not bool(payload.get("final")) or not task_id:
            return None

        message_payload = status.get("message")
        has_message_content = isinstance(message_payload, dict) and _parts_have_visible_content(message_payload.get("parts"))
        if not self.assistant_result_seen and not has_message_content:
            return None

        reference_id = build_a2a_coin_reference(profile_id=self.profile_id, task_id=task_id)
        if reference_id in self.charged_reference_ids:
            return None
        self.charged_reference_ids.add(reference_id)
        return PendingA2ACharge(
            profile_id=self.profile_id,
            task_id=task_id,
            conversation_id=self.conversation_id,
            prompt_text=self.prompt_text,
        )


def spend_intera_coins_for_a2a_completion(
    *,
    profile_id: str,
    task_id: str,
    conversation_id: str | None = None,
    prompt_text: str | None = None,
) -> dict[str, Any] | None:
    if not profile_id or not task_id:
        return None
    if not _parse_bool_env("KA2A_INTERA_COIN_SPEND_ENABLED", default=True):
        return None

    amount = _parse_int_env("KA2A_INTERA_COIN_COST", default=1)
    if amount <= 0:
        return None

    service_key = str(os.getenv("SUBSCRIPTION_SERVICE_KEY") or "").strip()
    if not service_key:
        logger.info("Skipping A2A coin spend because SUBSCRIPTION_SERVICE_KEY is not configured.")
        return None

    description = str(os.getenv("KA2A_INTERA_COIN_DESCRIPTION") or "A2A insight response").strip()[:255]
    metadata_bits = []
    if conversation_id:
        metadata_bits.append(f"conversation={conversation_id}")
    if prompt_text:
        preview = " ".join(str(prompt_text).strip().split())[:80]
        if preview:
            metadata_bits.append(f"prompt={preview}")
    if metadata_bits:
        detail = "; ".join(metadata_bits)
        description = f"{description} ({detail})"[:255]

    base_url = str(os.getenv("SUBSCRIPTION_SERVICE_URL") or "http://subscriptions:8550").rstrip("/")
    payload = {
        "profile_id": str(profile_id),
        "amount": amount,
        "description": description,
        "reference_id": build_a2a_coin_reference(profile_id=str(profile_id), task_id=str(task_id)),
        "action": "debit",
        "dry_run": False,
    }
    req = request.Request(
        f"{base_url}/internal/v1/coins/",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Intera-Service-Key": service_key,
        },
    )
    try:
        timeout = float(os.getenv("SUBSCRIPTION_SERVICE_TIMEOUT") or "2.0")
        with request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode())
    except error.HTTPError as exc:
        try:
            error_payload = json.loads(exc.read().decode())
        except Exception:
            error_payload = {"detail": str(exc)}
        logger.warning(
            "A2A coin spend rejected profile=%s task=%s status=%s payload=%s",
            profile_id,
            task_id,
            exc.code,
            error_payload,
        )
        return None
    except (error.URLError, TimeoutError, ValueError) as exc:
        logger.warning("A2A coin spend unavailable profile=%s task=%s: %s", profile_id, task_id, exc)
        return None
