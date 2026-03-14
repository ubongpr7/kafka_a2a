from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping


def _read_text(path: str | None) -> str | None:
    if not path:
        return None
    value = Path(path).read_text(encoding="utf-8").strip()
    return value or None


def resolve_system_prompt_from_env(env: Mapping[str, str] | None = None) -> str:
    env_map = env or os.environ
    prompt_path = (
        env_map.get("KA2A_SYSTEM_PROMPT_PATH")
        or env_map.get("KA2A_AGENT_SYSTEM_PROMPT_PATH")
        or ""
    ).strip()
    prompt_from_file = _read_text(prompt_path)
    if prompt_from_file:
        return prompt_from_file
    return (
        env_map.get("KA2A_SYSTEM_PROMPT")
        or env_map.get("KA2A_AGENT_SYSTEM_PROMPT")
        or ""
    ).strip()
