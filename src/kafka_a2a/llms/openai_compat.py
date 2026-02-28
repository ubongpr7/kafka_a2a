from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.request import Request, urlopen

from kafka_a2a.credentials import ResolvedLlmCredentials


def _require_langchain_core() -> Any:
    try:
        from langchain_core.messages import AIMessage  # noqa: F401
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "OpenAI-compatible LLM adapter requires the `lang` extra (e.g. `uv sync --extra lang`)."
        ) from exc
    from langchain_core.messages import AIMessage

    return AIMessage


def _endpoint(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("base_url is required for OpenAI-compatible LLMs (set KA2A_LLM_BASE_URL).")
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _to_openai_messages(messages: Iterable[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for msg in messages:
        role = getattr(msg, "type", None) or getattr(msg, "role", None) or ""
        role = str(role).lower()
        if role in ("human", "user"):
            role = "user"
        elif role in ("ai", "assistant"):
            role = "assistant"
        elif role in ("system",):
            role = "system"
        else:
            role = "user"

        content = getattr(msg, "content", None)
        if content is None:
            content = ""
        if not isinstance(content, str):
            content = str(content)
        out.append({"role": role, "content": content})
    return out


@dataclass(slots=True)
class OpenAICompatChatModel:
    """
    Minimal OpenAI-compatible Chat Completions client.

    This is intentionally lightweight and dependency-free (uses stdlib urllib).
    """

    base_url: str
    api_key: str
    model: str
    timeout_s: float = 60.0
    extra: dict[str, Any] | None = None

    async def ainvoke(self, messages: Iterable[Any], **_: Any) -> Any:
        AIMessage = _require_langchain_core()

        url = _endpoint(self.base_url)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _to_openai_messages(messages),
        }
        if self.extra:
            payload.update(self.extra)
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {
            "content-type": "application/json",
            "authorization": f"Bearer {self.api_key}",
        }

        def _post() -> bytes:
            req = Request(url, data=body, headers=headers, method="POST")
            with urlopen(req, timeout=float(self.timeout_s)) as resp:  # noqa: S310
                return resp.read()

        raw = await asyncio.to_thread(_post)
        data = json.loads(raw.decode("utf-8"))
        try:
            content = data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise RuntimeError(f"Unexpected OpenAI-compatible response: {data}") from exc
        return AIMessage(content=str(content))


def create_chat_model(
    creds: ResolvedLlmCredentials,
    *,
    metadata: dict[str, Any] | None = None,
) -> OpenAICompatChatModel:
    """
    Default `KA2A_LLM_FACTORY` target.

    Uses `ResolvedLlmCredentials` and returns an object with `ainvoke(messages) -> AIMessage`.
    """

    _ = metadata
    model = (creds.model or "").strip()
    if not model:
        raise ValueError("LLM model is required for OpenAI-compatible LLMs (set KA2A_LLM_MODEL).")
    return OpenAICompatChatModel(
        base_url=creds.base_url or "",
        api_key=creds.api_key,
        model=model,
        extra=creds.extra,
    )

