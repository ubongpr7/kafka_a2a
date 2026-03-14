from kafka_a2a.credentials import ResolvedLlmCredentials
from kafka_a2a.llms.openai_compat import create_chat_model


def test_create_chat_model_defaults_openai_base_url_for_chatgpt_provider() -> None:
    model = create_chat_model(
        ResolvedLlmCredentials(
            provider="chatgpt",
            api_key="test-key",
            model="gpt-5-mini",
            base_url=None,
        )
    )

    assert model.base_url == "https://api.openai.com"


def test_create_chat_model_defaults_xai_base_url_for_grok_provider() -> None:
    model = create_chat_model(
        ResolvedLlmCredentials(
            provider="grok",
            api_key="test-key",
            model="grok-4",
            base_url=None,
        )
    )

    assert model.base_url == "https://api.x.ai"
