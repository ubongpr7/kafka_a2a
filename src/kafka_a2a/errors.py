from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class A2AError(Exception):
    """
    Error compatible with A2A-style error payloads.

    For Kafka transport, this is typically serialized into an error object and sent
    in the response envelope.
    """

    code: int
    message: str
    data: Any | None = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            payload["data"] = self.data
        return payload


class A2AErrorCode:
    # JSON-RPC standard
    PARSE_ERROR = -32700
    INVALID_REQUEST = -32600
    METHOD_NOT_FOUND = -32601
    INVALID_PARAMS = -32602
    INTERNAL_ERROR = -32603

    # A2A-specific (per spec v0.3.0)
    TASK_NOT_FOUND = -32001
    TASK_NOT_CANCELABLE = -32002
    PUSH_NOTIFICATION_NOT_SUPPORTED = -32003
    UNSUPPORTED_OPERATION = -32004
    CONTENT_TYPE_NOT_SUPPORTED = -32005
    INVALID_AGENT_RESPONSE = -32006
    AUTHENTICATED_EXTENDED_CARD_NOT_CONFIGURED = -32007

    # K-A2A extensions (SaaS / multi-tenant)
    UNAUTHENTICATED = -32010
    PERMISSION_DENIED = -32011
