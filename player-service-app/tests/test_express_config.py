import secrets

import pytest

from express_api import API_PREFIX, IntegrationSettings


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"PLAYER_INTEGRATION_ENABLED": "false"},
        {
            "PLAYER_INTEGRATION_ENABLED": "false",
            "PLAYER_INTEGRATION_TOKEN": "unused",
            "PLAYER_DB_BUSY_TIMEOUT_SECONDS": "unused",
            "PLAYER_QUERY_TIMEOUT_SECONDS": "unused",
        },
    ],
)
def test_integration_is_disabled_by_default(environ):
    assert IntegrationSettings.from_environment(environ) is None


@pytest.mark.parametrize("value", ["", "TRUE", "False", "0", "1", "yes", " true "])
def test_invalid_enable_flag_fails_closed(value):
    with pytest.raises(ValueError, match="PLAYER_INTEGRATION_ENABLED"):
        IntegrationSettings.from_environment({"PLAYER_INTEGRATION_ENABLED": value})


def test_enabled_integration_requires_a_token():
    with pytest.raises(ValueError, match="PLAYER_INTEGRATION_TOKEN"):
        IntegrationSettings.from_environment({"PLAYER_INTEGRATION_ENABLED": "true"})


@pytest.mark.parametrize(
    "token",
    [
        "",
        "x" * 42,
        "x" * 257,
        "x" * 42 + " ",
        "x" * 42 + "\n",
        "x" * 42 + "\x00",
        "x" * 42 + "\u00e9",
        "x" * 42 + ".",
        "x" * 42 + "=",
        "Bearer " + "x" * 43,
    ],
)
def test_invalid_tokens_fail_without_echoing_the_value(token):
    with pytest.raises(ValueError, match="PLAYER_INTEGRATION_TOKEN") as caught:
        IntegrationSettings.from_environment(
            {"PLAYER_INTEGRATION_ENABLED": "true", "PLAYER_INTEGRATION_TOKEN": token}
        )
    if token:
        assert token not in str(caught.value)


@pytest.mark.parametrize("random_bytes", [32, 192])
def test_token_length_boundaries_and_safe_repr(random_bytes):
    token = secrets.token_urlsafe(random_bytes)
    settings = IntegrationSettings.from_environment(
        {"PLAYER_INTEGRATION_ENABLED": "true", "PLAYER_INTEGRATION_TOKEN": token}
    )

    assert settings.token == token
    assert token not in repr(settings)
    assert settings.busy_timeout_seconds == 1
    assert settings.query_timeout_seconds == 2


@pytest.mark.parametrize(
    "name", ["PLAYER_DB_BUSY_TIMEOUT_SECONDS", "PLAYER_QUERY_TIMEOUT_SECONDS"]
)
@pytest.mark.parametrize(
    "value", ["", "invalid", "nan", "inf", "-inf", "-1", "60.001", "1e999"]
)
def test_invalid_timeouts_fail_without_echoing_values(express_token, name, value):
    with pytest.raises(ValueError, match=name):
        IntegrationSettings.from_environment(
            {
                "PLAYER_INTEGRATION_ENABLED": "true",
                "PLAYER_INTEGRATION_TOKEN": express_token,
                name: value,
            }
        )


@pytest.mark.parametrize("value", ["0", "-0", "0.0009"])
def test_query_deadline_cannot_be_disabled(express_token, value):
    with pytest.raises(ValueError, match="PLAYER_QUERY_TIMEOUT_SECONDS"):
        IntegrationSettings.from_environment(
            {
                "PLAYER_INTEGRATION_ENABLED": "true",
                "PLAYER_INTEGRATION_TOKEN": express_token,
                "PLAYER_QUERY_TIMEOUT_SECONDS": value,
            }
        )


@pytest.mark.parametrize("busy", ["0", "0.001", "60"])
@pytest.mark.parametrize("query", ["0.001", "2", "60"])
def test_timeout_boundaries_are_accepted(express_token, busy, query):
    settings = IntegrationSettings.from_environment(
        {
            "PLAYER_INTEGRATION_ENABLED": "true",
            "PLAYER_INTEGRATION_TOKEN": express_token,
            "PLAYER_DB_BUSY_TIMEOUT_SECONDS": busy,
            "PLAYER_QUERY_TIMEOUT_SECONDS": query,
        }
    )
    assert settings.busy_timeout_seconds == float(busy)
    assert settings.query_timeout_seconds == float(query)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PLAYER_INTEGRATION_ENABLED", "yes"),
        ("PLAYER_INTEGRATION_TOKEN", "short"),
        ("PLAYER_DB_BUSY_TIMEOUT_SECONDS", "invalid"),
        ("PLAYER_QUERY_TIMEOUT_SECONDS", "0"),
    ],
)
def test_bad_configuration_fails_before_database_initialization(
    client, monkeypatch, express_token, name, value
):
    import app

    def unexpected_database_initialization():
        pytest.fail("Invalid configuration must fail before database initialization")

    monkeypatch.setattr(app, "ensure_player_db", unexpected_database_initialization)
    monkeypatch.setenv("PLAYER_INTEGRATION_ENABLED", "true")
    monkeypatch.setenv("PLAYER_INTEGRATION_TOKEN", express_token)
    monkeypatch.delenv("PLAYER_DB_BUSY_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("PLAYER_QUERY_TIMEOUT_SECONDS", raising=False)
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        app.create_app()


def test_default_app_has_no_integration_routes_or_headers(client):
    response = client.get(API_PREFIX + "/players")
    assert response.status_code == 404
    assert not response.is_json
    assert "X-Request-ID" not in response.headers
    assert all(
        not rule.rule.startswith(API_PREFIX) for rule in client.application.url_map.iter_rules()
    )


def test_settings_are_captured_per_application(
    express_client, express_headers, monkeypatch
):
    import app

    replacement = secrets.token_urlsafe(32)
    monkeypatch.setenv("PLAYER_INTEGRATION_TOKEN", replacement)
    with app.create_app().test_client() as replacement_client:
        assert replacement_client.get(
            API_PREFIX + "/players", headers=express_headers
        ).status_code == 401
        assert replacement_client.get(
            API_PREFIX + "/players",
            headers={"Authorization": f"Bearer {replacement}"},
        ).status_code == 200
    assert express_client.get(
        API_PREFIX + "/players", headers=express_headers
    ).status_code == 200
    assert express_client.get(
        API_PREFIX + "/players", headers={"Authorization": f"Bearer {replacement}"}
    ).status_code == 401

    monkeypatch.setenv("PLAYER_INTEGRATION_ENABLED", "false")
    with app.create_app().test_client() as disabled_client:
        response = disabled_client.get(API_PREFIX + "/players")
        assert response.status_code == 404
        assert "X-Request-ID" not in response.headers
