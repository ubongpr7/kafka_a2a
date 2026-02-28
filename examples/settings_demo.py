from __future__ import annotations

from kafka_a2a.settings import Ka2aSettings


def main() -> None:
    settings = Ka2aSettings.from_env()
    creds = settings.resolve_llm_credentials()
    if creds is None:
        print("No LLM credentials configured (set KA2A_LLM_PROVIDER + KA2A_LLM_API_KEY).")
        return
    print(
        "LLM credentials resolved:",
        {"provider": creds.provider, "model": creds.model, "base_url": creds.base_url, "api_key_len": len(creds.api_key)},
    )


if __name__ == "__main__":
    main()

