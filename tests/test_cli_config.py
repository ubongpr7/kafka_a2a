from kafka_a2a.cli import _gateway_stream_timeout_s_from_env, _jwt_from_env


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


def test_gateway_stream_timeout_uses_long_default_and_supports_explicit_disable(monkeypatch) -> None:
    monkeypatch.delenv("KA2A_GATEWAY_STREAM_REQUEST_TIMEOUT_S", raising=False)
    assert _gateway_stream_timeout_s_from_env() == 900.0

    monkeypatch.setenv("KA2A_GATEWAY_STREAM_REQUEST_TIMEOUT_S", "0")
    assert _gateway_stream_timeout_s_from_env() is None
