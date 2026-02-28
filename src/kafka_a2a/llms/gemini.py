from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Iterable
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from kafka_a2a.credentials import ResolvedLlmCredentials
from kafka_a2a.llms.chat_model import ChatResponse
from kafka_a2a.llms.controls import RetryConfig, backoff_delay_s, llm_semaphore


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

    def _as_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return str(value)

    def _to_parts(content: Any) -> list[dict[str, Any]]:
        if content is None:
            return [{"text": ""}]
        if isinstance(content, str):
            return [{"text": content}]
        if isinstance(content, list):
            out: list[dict[str, Any]] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                kind = str(item.get("kind") or item.get("type") or "").strip().lower()
                if kind in ("text",):
                    out.append({"text": _as_text(item.get("text"))})
                    continue
                if kind in ("file",):
                    file_obj = item.get("file") if isinstance(item.get("file"), dict) else {}
                    mime = _as_text(
                        file_obj.get("mime_type") or file_obj.get("mimeType") or "application/octet-stream"
                    )
                    if isinstance(file_obj.get("bytes"), str) and file_obj.get("bytes"):
                        out.append({"inline_data": {"mime_type": mime, "data": file_obj["bytes"]}})
                        continue
                    if isinstance(file_obj.get("uri"), str) and file_obj.get("uri"):
                        out.append({"file_data": {"mime_type": mime, "file_uri": file_obj["uri"]}})
                        continue
                    out.append({"text": f"[file mime={mime}]"})
                    continue
                if kind in ("image_url",):
                    image_url = item.get("image_url") if isinstance(item.get("image_url"), dict) else {}
                    url = _as_text(image_url.get("url") or item.get("url"))
                    if url.startswith("data:") and ";base64," in url:
                        meta, b64 = url.split(";base64,", 1)
                        mime = meta[len("data:") :] or "application/octet-stream"
                        out.append({"inline_data": {"mime_type": mime, "data": b64}})
                        continue
                    out.append({"file_data": {"mime_type": "image/*", "file_uri": url}})
                    continue
                if kind in ("data",):
                    out.append({"text": json.dumps(item.get("data"), ensure_ascii=False)})
                    continue
                if kind in ("tool-call", "tool-result"):
                    out.append({"text": json.dumps(item, ensure_ascii=False)})
                    continue
                out.append({"text": f"[{kind or 'part'}]"})
            return out or [{"text": ""}]
        return [{"text": _as_text(content)}]

    for msg in messages:
        msg_type = getattr(msg, "type", None) or getattr(msg, "role", None) or ""
        msg_type = str(msg_type).lower()
        content = getattr(msg, "content", None)

        if msg_type in ("system",):
            if content is None:
                continue
            if isinstance(content, str):
                if content:
                    system_parts.append(content)
                continue
            if isinstance(content, list):
                for p in _to_parts(content):
                    text = p.get("text")
                    if isinstance(text, str) and text.strip():
                        system_parts.append(text.strip())
                continue
            text = _as_text(content).strip()
            if text:
                system_parts.append(text)
            continue

        role = "user"
        if msg_type in ("ai", "assistant", "model"):
            role = "model"
        elif msg_type in ("human", "user"):
            role = "user"

        contents.append({"role": role, "parts": _to_parts(content)})

    system_text = "\n\n".join(system_parts).strip() or None
    return system_text, contents


class UpstreamHttpError(RuntimeError):
    def __init__(self, *, status: int, body: str, headers: dict[str, str] | None = None) -> None:
        super().__init__(f"HTTP {status}")
        self.status = int(status)
        self.body = body
        self.headers = headers or {}


@dataclass(slots=True)
class GeminiChatModel:
    """
    Minimal Gemini GenerateContent client (REST).

    Dependency-free (stdlib urllib). Returns a provider-agnostic `ChatResponse` from `ainvoke()`.
    """

    api_key: str
    model: str
    base_url: str | None = None
    timeout_s: float = 60.0
    extra: dict[str, Any] | None = None

    async def ainvoke(self, messages: Iterable[Any], **_: Any) -> ChatResponse:
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
                raise UpstreamHttpError(status=int(getattr(exc, "code", 0) or 0), body=err_body, headers=err_headers) from exc

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
                        raise RuntimeError(f"Gemini upstream error ({exc.status}): {detail}") from exc
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
            parts = data["candidates"][0]["content"]["parts"]
        except Exception as exc:
            raise RuntimeError(f"Unexpected Gemini response: {data}") from exc

        out_parts: list[dict[str, Any]] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text:
                out_parts.append({"kind": "text", "text": text})
                continue
            inline_data = part.get("inline_data") or part.get("inlineData")
            if isinstance(inline_data, dict):
                mime = inline_data.get("mime_type") or inline_data.get("mimeType") or "application/octet-stream"
                b64 = inline_data.get("data") or ""
                if isinstance(b64, str) and b64:
                    out_parts.append({"kind": "file", "file": {"bytes": b64, "mimeType": str(mime)}})
                    continue
            file_data = part.get("file_data") or part.get("fileData")
            if isinstance(file_data, dict):
                mime = file_data.get("mime_type") or file_data.get("mimeType") or "application/octet-stream"
                uri = file_data.get("file_uri") or file_data.get("fileUri") or ""
                if isinstance(uri, str) and uri:
                    out_parts.append({"kind": "file", "file": {"uri": uri, "mimeType": str(mime)}})
                    continue
            fn = part.get("function_call") or part.get("functionCall")
            if isinstance(fn, dict):
                name = fn.get("name") or ""
                args = fn.get("args") or fn.get("arguments") or {}
                if isinstance(name, str) and name:
                    out_parts.append(
                        {"kind": "tool-call", "name": name, "arguments": args if isinstance(args, dict) else {"value": args}}
                    )
                    continue

        # Preserve multimodal outputs as a list; otherwise collapse to a string.
        non_text = any((p.get("kind") or "").lower() != "text" for p in out_parts)
        if non_text:
            return ChatResponse(content=out_parts, raw=data)
        text_out = "".join([str(p.get("text") or "") for p in out_parts]).strip()
        return ChatResponse(content=text_out, raw=data)


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
