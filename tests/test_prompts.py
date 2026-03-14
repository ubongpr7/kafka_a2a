from pathlib import Path

from kafka_a2a.prompts import resolve_system_prompt_from_env


def test_resolve_system_prompt_from_env_prefers_prompt_path(tmp_path: Path) -> None:
    prompt_path = tmp_path / "host.txt"
    prompt_path.write_text("Host prompt from file", encoding="utf-8")

    resolved = resolve_system_prompt_from_env(
        {
            "KA2A_SYSTEM_PROMPT_PATH": str(prompt_path),
            "KA2A_SYSTEM_PROMPT": "Fallback prompt",
        }
    )

    assert resolved == "Host prompt from file"


def test_resolve_system_prompt_from_env_falls_back_to_inline_value() -> None:
    resolved = resolve_system_prompt_from_env({"KA2A_SYSTEM_PROMPT": "Inline prompt"})
    assert resolved == "Inline prompt"
