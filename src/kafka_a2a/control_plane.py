from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


class ControlPlaneError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, payload: Any | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


def _strip_trailing_slash(value: str) -> str:
    return value.rstrip("/")


def _default_base_url(env_map: dict[str, str]) -> str | None:
    explicit = _strip_trailing_slash((env_map.get("KA2A_CONTROL_PLANE_BASE_URL") or "").strip())
    if explicit:
        return explicit
    runtime_shared_token = (env_map.get("KA2A_RUNTIME_SHARED_TOKEN") or "").strip()
    if not runtime_shared_token:
        return None
    host = (env_map.get("KA2A_CONTROL_PLANE_HOST") or "127.0.0.1").strip() or "127.0.0.1"
    port = (env_map.get("KA2A_GATEWAY_PORT") or "7006").strip() or "7006"
    return f"http://{host}:{port}"


@dataclass(slots=True)
class ControlPlaneClientConfig:
    base_url: str | None = None
    runtime_shared_token: str | None = None
    timeout_s: float = 10.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "ControlPlaneClientConfig":
        env_map = env or os.environ
        return cls(
            base_url=_default_base_url(env_map),
            runtime_shared_token=(env_map.get("KA2A_RUNTIME_SHARED_TOKEN") or "").strip() or None,
            timeout_s=float(env_map.get("KA2A_CONTROL_PLANE_TIMEOUT_S") or "10"),
        )

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)


class ControlPlaneClient:
    def __init__(self, *, config: ControlPlaneClientConfig | None = None) -> None:
        self._cfg = config or ControlPlaneClientConfig.from_env()

    @property
    def enabled(self) -> bool:
        return self._cfg.enabled

    def _require_base_url(self) -> str:
        if not self._cfg.base_url:
            raise ControlPlaneError("Control-plane base URL is not configured.")
        return self._cfg.base_url

    def _request_json(self, *, path: str, headers: dict[str, str] | None = None) -> Any:
        url = f"{self._require_base_url()}{path}"
        req = Request(url, headers=headers or {}, method="GET")
        try:
            with urlopen(req, timeout=self._cfg.timeout_s) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw.strip() else {}
        except TimeoutError as exc:
            raise ControlPlaneError(
                f"Control-plane request timed out after {self._cfg.timeout_s:.1f}s.",
            ) from exc
        except HTTPError as exc:
            payload_text = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(payload_text) if payload_text.strip() else None
            except Exception:
                payload = payload_text or None
            raise ControlPlaneError(
                f"Control-plane request failed with HTTP {exc.code}.",
                status_code=exc.code,
                payload=payload,
            ) from exc
        except URLError as exc:
            raise ControlPlaneError(f"Unable to reach control-plane service: {exc.reason}") from exc

    def list_workspace_runtime_registry(self, *, authorization: str) -> dict[str, Any]:
        return self._request_json(
            path="/agent_api/runtime/agents/registry/",
            headers={"Authorization": authorization},
        )

    def get_workspace_agent_config(self, *, slug: str, authorization: str) -> dict[str, Any]:
        return self._request_json(
            path=f"/agent_api/runtime/agents/{quote(slug, safe='')}/config/",
            headers={"Authorization": authorization},
        )

    def get_workspace_agent_card(self, *, slug: str, authorization: str) -> dict[str, Any]:
        return self._request_json(
            path=f"/agent_api/runtime/agents/{quote(slug, safe='')}/card/",
            headers={"Authorization": authorization},
        )

    def list_internal_runtime_registry(self) -> dict[str, Any]:
        headers: dict[str, str] = {}
        if self._cfg.runtime_shared_token:
            headers["X-KA2A-Runtime-Token"] = self._cfg.runtime_shared_token
        return self._request_json(
            path="/agent_api/runtime/internal/registry/",
            headers=headers,
        )
