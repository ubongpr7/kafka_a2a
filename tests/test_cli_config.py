from kafka_a2a.cli import _jwt_from_env


def test_jwt_from_env_normalizes_escaped_pem_newlines(monkeypatch):
    monkeypatch.setenv("KA2A_JWT_ENABLED", "true")
    monkeypatch.setenv(
        "KA2A_JWT_KEY",
        "-----BEGIN PUBLIC KEY-----\\nabc123\\n-----END PUBLIC KEY-----",
    )
    monkeypatch.setenv("KA2A_JWT_ALGORITHMS", "RS256")

    config = _jwt_from_env()

    assert config is not None
    assert config.secret == "-----BEGIN PUBLIC KEY-----\nabc123\n-----END PUBLIC KEY-----"
    assert config.algorithms == ["RS256"]
