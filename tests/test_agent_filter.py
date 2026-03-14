from kafka_a2a.agent_filter import filter_agent_cards
from kafka_a2a.models import AgentCard


def _card(name: str) -> AgentCard:
    return AgentCard(name=name, description=f"{name} agent", url=f"kafka://{name}", version="0.1.0")


def test_filter_agent_cards_respects_allowed_env(monkeypatch) -> None:
    monkeypatch.setenv("KA2A_ALLOWED_DOWNSTREAM_AGENTS", "product")

    cards = filter_agent_cards(
        [_card("host"), _card("product"), _card("echo"), _card("weather")],
        exclude_names={"host"},
    )

    assert [card.name for card in cards] == ["product"]


def test_filter_agent_cards_can_include_default_agent(monkeypatch) -> None:
    monkeypatch.setenv("KA2A_ALLOWED_DOWNSTREAM_AGENTS", "product")

    cards = filter_agent_cards(
        [_card("host"), _card("product"), _card("echo")],
        include_names={"host"},
    )

    assert [card.name for card in cards] == ["host", "product"]
