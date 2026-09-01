from kafka_a2a.agent_filter import card_profile_id, card_public_slug, filter_agent_cards
from kafka_a2a.models import AgentCard


def _card(name: str, *, public_slug: str | None = None, profile_id: str | None = None) -> AgentCard:
    payload = {
        "name": name,
        "description": f"{name} agent",
        "url": f"kafka://{name}",
        "version": "0.1.0",
    }
    if public_slug or profile_id:
        payload["metadata"] = {
            "ka2aRuntime": {
                "publicSlug": public_slug or name,
                "profileId": profile_id,
            }
        }
    return AgentCard(**payload)


def _registry_card(
    name: str,
    *,
    slug: str,
    profile: str | None = None,
) -> AgentCard:
    return AgentCard.model_validate(
        {
            "name": name,
            "slug": slug,
            "profile": profile,
            "description": f"{name} agent",
            "url": f"local://{slug}",
            "version": "0.1.0",
            "capabilities": {"streaming": True, "pushNotifications": True},
            "default_input_modes": ["text"],
            "default_output_modes": ["text"],
            "security_schemes": {},
            "security": [],
        }
    )


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


def test_filter_agent_cards_can_scope_by_workspace_profile() -> None:
    cards = filter_agent_cards(
        [
            _card("wa-p1-host-abc", public_slug="host", profile_id="1"),
            _card("wa-p1-inventory-abc", public_slug="inventory", profile_id="1"),
            _card("legacy-host"),
            _card("wa-p2-host-def", public_slug="host", profile_id="2"),
        ],
        required_profile_id="1",
    )

    assert [card_public_slug(card) for card in cards] == ["host", "inventory"]
    assert {card_profile_id(card) for card in cards} == {"1"}


def test_filter_agent_cards_can_use_public_slug_allowlist() -> None:
    cards = filter_agent_cards(
        [
            _card("wa-p1-host-abc", public_slug="host", profile_id="1"),
            _card("wa-p1-users-abc", public_slug="users", profile_id="1"),
            _card("wa-p1-product-abc", public_slug="product", profile_id="1"),
        ],
        required_profile_id="1",
        allowed_public_slugs={"users"},
    )

    assert [card_public_slug(card) for card in cards] == ["users"]


def test_filter_agent_cards_allow_parent_slug_to_expose_child_specialists() -> None:
    cards = filter_agent_cards(
        [
            _card("wa-p1-host-abc", public_slug="host", profile_id="1"),
            _card("wa-p1-inventory-fulfillment-abc", public_slug="inventory_fulfillment", profile_id="1"),
            _card("wa-p1-inventory-visibility-abc", public_slug="inventory_visibility", profile_id="1"),
            _card("wa-p1-product-discovery-abc", public_slug="product_discovery", profile_id="1"),
        ],
        required_profile_id="1",
        allowed_public_slugs={"inventory"},
    )

    assert [card_public_slug(card) for card in cards] == [
        "inventory_fulfillment",
        "inventory_visibility",
    ]


def test_card_public_slug_falls_back_to_registry_slug() -> None:
    card = _registry_card("Host", slug="host", profile="4")

    assert card_public_slug(card) == "host"


def test_card_profile_id_falls_back_to_registry_profile() -> None:
    card = _registry_card("Host", slug="host", profile="4")

    assert card_profile_id(card) == "4"


def test_filter_agent_cards_can_scope_db_registry_records_by_profile() -> None:
    cards = filter_agent_cards(
        [
            _registry_card("Host", slug="host", profile="4"),
            _registry_card("POS", slug="pos", profile="4"),
            _registry_card("Users", slug="users", profile="7"),
        ],
        required_profile_id="4",
    )

    assert [card_public_slug(card) for card in cards] == ["host", "pos"]


def test_filter_agent_cards_allow_specific_env_allowlist_to_expose_generic_public_slug(monkeypatch) -> None:
    monkeypatch.setenv(
        "KA2A_ALLOWED_DOWNSTREAM_AGENTS",
        "host,pos_admin,pos_live,product_discovery,product_catalog_admin,inventory_visibility",
    )

    cards = filter_agent_cards(
        [
            _card("wa-p1-pos-live-abc", public_slug="pos", profile_id="1"),
            _card("wa-p1-product-discovery-abc", public_slug="product", profile_id="1"),
            _card("wa-p1-inventory-visibility-abc", public_slug="inventory", profile_id="1"),
            _card("wa-p1-users-abc", public_slug="users", profile_id="1"),
        ],
        required_profile_id="1",
    )

    assert [card_public_slug(card) for card in cards] == ["pos", "product", "inventory"]


def test_filter_agent_cards_allow_specific_env_allowlist_to_expose_generic_name(monkeypatch) -> None:
    monkeypatch.setenv("KA2A_ALLOWED_DOWNSTREAM_AGENTS", "pos_admin,product_discovery")

    cards = filter_agent_cards(
        [
            _card("pos"),
            _card("product"),
            _card("users"),
        ],
    )

    assert [card.name for card in cards] == ["pos", "product"]
