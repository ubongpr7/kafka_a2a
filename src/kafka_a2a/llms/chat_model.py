from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Iterable, Protocol, runtime_checkable


ChatContent = str | list[dict[str, Any]]


@dataclass(slots=True)
class ChatResponse:
    """
    Provider-agnostic chat response.

    `content` is either:
      - a plain string (text-only), or
      - a list of dict parts (multimodal; K-A2A-like `{kind: ...}` items).
    """

    content: ChatContent
    raw: Any | None = None


class ChatModel(Protocol):
    async def ainvoke(self, messages: Iterable[Any], **kwargs: Any) -> ChatResponse: ...


@runtime_checkable
class StreamingChatModel(Protocol):
    async def astream(self, messages: Iterable[Any], **kwargs: Any) -> AsyncIterator[ChatResponse]: ...

