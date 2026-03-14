from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from kafka_a2a.credentials import ResolvedLlmCredentials
from kafka_a2a.llms.chat_model import ChatResponse
from kafka_a2a.llms.controls import RetryConfig, backoff_delay_s, llm_semaphore


def _endpoint(base_url: str) -> str:
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("base_url is required for OpenAI-compatible LLMs (set KA2A_LLM_BASE_URL).")
    if base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


def _default_base_url_for_provider(provider: str | None) -> str | None:
    provider_lower = (provider or "").strip().lower()
    if provider_lower in ("openai", "chatgpt", "openai_compat", "openai-compatible", "openai-compatible-api"):
        return "https://api.openai.com"
    if provider_lower in ("xai", "grok"):
        return "https://api.x.ai"
    return None


def _to_openai_messages(messages: Iterable[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def _as_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return str(value)

    def _data_url(*, mime: str, b64: str) -> str:
        return f"data:{mime};base64,{b64}"

    def _to_content(value: Any) -> Any:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            parts_out: list[dict[str, Any]] = []
            for item in value:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("type") or item.get("kind") or "").strip().lower()
                if kind == "text":
                    parts_out.append({"type": "text", "text": _as_text(item.get("text"))})
                    continue
                if kind == "image_url":
                    # Already in OpenAI format; pass through.
                    image_url = item.get("image_url")
                    if isinstance(image_url, dict):
                        parts_out.append({"type": "image_url", "image_url": image_url})
                    continue
                if kind == "file":
                    file_obj = item.get("file") if isinstance(item.get("file"), dict) else {}
                    mime = _as_text(
                        file_obj.get("mime_type") or file_obj.get("mimeType") or "application/octet-stream"
                    )
                    if isinstance(file_obj.get("bytes"), str) and file_obj.get("bytes"):
                        b64 = file_obj["bytes"]
                        if mime.startswith("image/"):
                            parts_out.append(
                                {"type": "image_url", "image_url": {"url": _data_url(mime=mime, b64=b64)}}
                            )
                        else:
                            parts_out.append(
                                {"type": "text", "text": f"[file mime={mime} bytes={len(b64)}b]"}
                            )
                        continue
                    if isinstance(file_obj.get("uri"), str) and file_obj.get("uri"):
                        uri = file_obj["uri"]
                        if mime.startswith("image/"):
                            parts_out.append({"type": "image_url", "image_url": {"url": uri}})
                        else:
                            parts_out.append({"type": "text", "text": f"[file uri={uri} mime={mime}]"})
                        continue
                    parts_out.append({"type": "text", "text": f"[file mime={mime}]"})
                    continue
                if kind == "data":
                    parts_out.append({"type": "text", "text": json.dumps(item.get("data"), ensure_ascii=False)})
                    continue
                if kind in ("tool-call", "tool-result"):
                    parts_out.append({"type": "text", "text": json.dumps(item, ensure_ascii=False)})
                    continue
                parts_out.append({"type": "text", "text": f"[{kind or 'part'}]"})

            # If this is effectively text-only, keep it as a string for maximum compatibility.
            text_only = all(p.get("type") == "text" for p in parts_out)
            if text_only:
                return "\n".join([_as_text(p.get("text")) for p in parts_out]).strip()
            return parts_out

        return _as_text(value)

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
        out.append({"role": role, "content": _to_content(content)})
    return out


class UpstreamHttpError(RuntimeError):
    def __init__(self, *, status: int, body: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = int(status)
        self.body = body
        self.headers = headers or {}


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

    async def ainvoke(self, messages: Iterable[Any], **_: Any) -> ChatResponse:
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
            try:
                with urlopen(req, timeout=float(self.timeout_s)) as resp:  # noqa: S310
                    return resp.read()
            except HTTPError as exc:
                err_body = ""
                try:
                    err_body = exc.read().decode("utf-8", errors="replace")
                except Exception:
                    err_body = ""
                err_headers = {str(k): str(v) for k, v in dict(getattr(exc, "headers", {}) or {}).items()}
                raise UpstreamHttpError(
                    status=int(getattr(exc, "code", 0) or 0),
                    body=err_body,
                    headers=err_headers,
                ) from exc

        retry_cfg = RetryConfig.from_env()

        async def _post_with_retries() -> bytes:
            attempt = 0
            while True:
                try:
                    return await asyncio.to_thread(_post)
                except UpstreamHttpError as exc:
                    retryable = exc.status in (408, 409, 425, 429, 500, 502, 503, 504)
                    if attempt >= max(0, retry_cfg.max_retries) or not retryable:
                        detail = exc.body.strip() or str(exc)
                        raise RuntimeError(f"OpenAI-compatible upstream error ({exc.status}): {detail}") from exc
                    retry_after_s: float | None = None
                    ra = (exc.headers.get("Retry-After") or exc.headers.get("retry-after") or "").strip()
                    if ra:
                        try:
                            retry_after_s = float(ra)
                        except Exception:
                            retry_after_s = None
                    await asyncio.sleep(backoff_delay_s(attempt=attempt, cfg=retry_cfg, retry_after_s=retry_after_s))
                    attempt += 1

        sem = llm_semaphore()
        if sem is None:
            raw = await _post_with_retries()
        else:
            async with sem:
                raw = await _post_with_retries()

        data = json.loads(raw.decode("utf-8"))
        try:
            content = data["choices"][0]["message"]["content"]
        except Exception as exc:
            raise RuntimeError(f"Unexpected OpenAI-compatible response: {data}") from exc
        return ChatResponse(content=str(content), raw=data)


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
    base_url = (creds.base_url or "").strip() or _default_base_url_for_provider(creds.provider) or ""
    return OpenAICompatChatModel(
        base_url=base_url,
        api_key=creds.api_key,
        model=model,
        extra=creds.extra,
    )
