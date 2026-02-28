from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from kafka_a2a.credentials import ResolvedLlmCredentials


def _require_langchain_core() -> Any:
    try:
        from langchain_core.messages import AIMessage  # noqa: F401
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "Gemini LLM adapter requires the `lang` extra (e.g. `uv sync --extra lang`)."
        ) from exc
    from langchain_core.messages import AIMessage

    return AIMessage


def _normalize_base_url(base_url: str | None) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        base = "https://generativelanguage.googleapis.com"
    return base


def _endpoint(base_url: str, model: str) -> str:
    base = _normalize_base_url(base_url)
    # If the user provides a versioned base, keep it. Otherwise default to v1beta.
    if base.endswith("/v1") or base.endswith("/v1beta"):
        return f"{base}/models/{model}:generateContent"
    return f"{base}/v1beta/models/{model}:generateContent"


def _to_gemini_contents(messages: Iterable[Any]) -> tuple[str | None, list[dict[str, Any]]]:
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []

    for msg in messages:
        msg_type = getattr(msg, "type", None) or getattr(msg, "role", None) or ""
        msg_type = str(msg_type).lower()
        content = getattr(msg, "content", None)
        if content is None:
            content = ""
        if not isinstance(content, str):
            content = str(content)

        if msg_type in ("system",):
            if content:
                system_parts.append(content)
            continue

        role = "user"
        if msg_type in ("ai", "assistant", "model"):
            role = "model"
        elif msg_type in ("human", "user"):
            role = "user"

        contents.append({"role": role, "parts": [{"text": content}]})

    system_text = "\n\n".join(system_parts).strip() or None
    return system_text, contents


@dataclass(slots=True)
class GeminiChatModel:
    """
    Minimal Gemini GenerateContent client (REST).

    Dependency-free (stdlib urllib). Returns a LangChain `AIMessage` from `ainvoke()`.
    """

    api_key: str
    model: str
    base_url: str | None = None
    timeout_s: float = 60.0
    extra: dict[str, Any] | None = None

    async def ainvoke(self, messages: Iterable[Any], **_: Any) -> Any:
        AIMessage = _require_langchain_core()

        system_text, contents = _to_gemini_contents(messages)
        if not contents:
            contents = [{"role": "user", "parts": [{"text": ""}]}]

        payload: dict[str, Any] = {"contents": contents}
        if system_text:
            payload["systemInstruction"] = {"parts": [{"text": system_text}]}

        # Allow passing Gemini request options via creds.extra (e.g. generationConfig).
        if self.extra:
            payload.update(self.extra)

        url = _endpoint(self.base_url, self.model)
        url = f"{url}?{urlencode({'key': self.api_key})}"
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"content-type": "application/json"}

        def _post() -> bytes:
            req = Request(url, data=body, headers=headers, method="POST")
            with urlopen(req, timeout=float(self.timeout_s)) as resp:  # noqa: S310
                return resp.read()

        raw = await asyncio.to_thread(_post)
        data = json.loads(raw.decode("utf-8"))

        try:
            parts = data["candidates"][0]["content"]["parts"]
        except Exception as exc:
            raise RuntimeError(f"Unexpected Gemini response: {data}") from exc

        texts: list[str] = []
        for part in parts:
            text = part.get("text")
            if text:
                texts.append(str(text))
        return AIMessage(content="".join(texts))


def create_chat_model(
    creds: ResolvedLlmCredentials,
    *,
    metadata: dict[str, Any] | None = None,
) -> GeminiChatModel:
    """
    `KA2A_LLM_FACTORY` target for Gemini.

    Env example:
      - KA2A_LLM_PROVIDER=gemini
      - KA2A_LLM_MODEL=gemini-1.5-flash
      - GOOGLE_API_KEY=...
    """

    _ = metadata
    model = (creds.model or "").strip()
    if not model:
        raise ValueError("Gemini model is required (set KA2A_LLM_MODEL).")
    return GeminiChatModel(
        api_key=creds.api_key,
        model=model,
        base_url=creds.base_url,
        extra=creds.extra,
    )

