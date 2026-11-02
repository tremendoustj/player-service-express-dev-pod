import json
import sqlite3
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

import pytest
from openapi_schema_validator import OAS31Validator
from openapi_spec_validator import OpenAPIV31SpecValidator

import express_api
import player_service
from express_api import API_PREFIX
from player_service import PlayerQueryTimeoutError, PlayerService


OPERATIONS = {
    "search_players": (API_PREFIX + "/players", API_PREFIX + "/players"),
    "get_player": (
        API_PREFIX + "/players/{player_id}",
        API_PREFIX + "/players/aaronha01",
    ),
    "get_player_statistics": (API_PREFIX + "/statistics", API_PREFIX + "/statistics"),
    "rank_players": (API_PREFIX + "/rankings", API_PREFIX + "/rankings?metric=height"),
    "compare_players": (
        API_PREFIX + "/comparisons",
        API_PREFIX + "/comparisons?player_ids=aaronto01,aaronha01",
    ),
}


def _unique_object(pairs):
    result = dict(pairs)
    assert len(result) == len(pairs), "Duplicate JSON object key in the contract"
    return result


@pytest.fixture(scope="session")
def contract():
    path = Path(__file__).resolve().parents[2] / "docs" / "express-openapi.json"
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)


@pytest.fixture(scope="session")
def schema_validator(contract):
    return OAS31Validator(contract)


def _objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


def _resolve(contract, value):
    if "$ref" not in value:
        return value
    reference = value["$ref"]
    assert reference.startswith("#/components/")
    target = contract
    for part in reference[2:].split("/"):
        target = target[part.replace("~1", "/").replace("~0", "~")]
    return target


def _operation(contract, operation):
    return contract["paths"][OPERATIONS[operation][0]]["get"]


def _parameters(contract, operation):
    path_item = contract["paths"][OPERATIONS[operation][0]]
    return [
        _resolve(contract, parameter)
        for parameter in path_item.get("parameters", [])
        + path_item["get"].get("parameters", [])
    ]


def _response_definition(contract, operation, status):
    return _resolve(contract, _operation(contract, operation)["responses"][str(status)])


def _assert_headers(response, definition, contract, schema_validator):
    for name, reference in definition["headers"].items():
        header = _resolve(contract, reference)
        assert header["required"] is True
        assert name in response.headers
        schema_validator.evolve(schema=header["schema"]).validate(response.headers[name])


def _reject_nonfinite(value):
    raise AssertionError(f"Nonstandard JSON numeric constant: {value}")


def _assert_response(response, definition, contract, schema_validator):
    assert response.mimetype == "application/json"
    payload = json.loads(response.get_data(as_text=True), parse_constant=_reject_nonfinite)
    schema = definition["content"]["application/json"]["schema"]
    schema_validator.evolve(schema=schema).validate(payload)
    _assert_headers(response, definition, contract, schema_validator)
    assert payload["request_id"] == response.headers["X-Request-ID"]
    return payload


def test_contract_is_valid_openapi_with_only_resolvable_local_references(contract):
    assert contract["openapi"] == "3.1.0"
    for node in _objects(contract):
        if "$ref" in node:
            _resolve(contract, node)
    OpenAPIV31SpecValidator(contract).validate()


def test_contract_covers_exactly_the_enabled_read_operations(contract, express_app):
    routes = {
        rule.rule.replace("<string:player_id>", "{player_id}"): rule
        for rule in express_app.url_map.iter_rules()
        if rule.endpoint.startswith("express_integration.")
        and rule.endpoint != "express_integration.get_metrics"
    }
    assert set(contract["paths"]) == set(routes) == {
        path for path, _ in OPERATIONS.values()
    }
    assert API_PREFIX + "/metrics" not in contract["paths"]
    metrics_rule = next(
        rule for rule in express_app.url_map.iter_rules()
        if rule.endpoint == "express_integration.get_metrics"
    )
    assert metrics_rule.rule == API_PREFIX + "/metrics"
    assert metrics_rule.methods == {"GET", "HEAD", "OPTIONS"}
    assert contract["security"] == [{"PlayerServiceBearer": []}]
    assert contract["components"]["securitySchemes"]["PlayerServiceBearer"]["type"] == "http"
    assert contract["components"]["securitySchemes"]["PlayerServiceBearer"]["scheme"] == "bearer"
    assert [server["url"] for server in contract["servers"]] == ["/"]
    operation_ids = []
    for path, path_item in contract["paths"].items():
        assert set(path_item) == {"parameters", "get"}
        assert routes[path].methods == {"GET", "HEAD", "OPTIONS"}
        operation = path_item["get"]
        operation_ids.append(operation["operationId"])
        assert operation["operationId"] == routes[path].endpoint.split(".")[1]
        assert "requestBody" not in operation
        assert "security" not in operation
        assert "servers" not in operation
        statuses = {"200", "400", "401", "414", "500", "503", "504"}
        if operation["operationId"] in ("get_player", "compare_players"):
            statuses.add("404")
        assert set(operation["responses"]) == statuses
    assert len(operation_ids) == len(set(operation_ids))


@pytest.mark.parametrize("operation", OPERATIONS)
def test_contract_parameters_match_runtime_allowlists(contract, operation):
    expected_queries = {
        "search_players": express_api._SEARCH_PARAMETERS,
        "get_player": frozenset(),
        "get_player_statistics": express_api._STATISTICS_PARAMETERS,
        "rank_players": express_api._RANKING_PARAMETERS,
        "compare_players": express_api._COMPARISON_PARAMETERS,
    }
    parameters = _parameters(contract, operation)
    identities = [(parameter["in"], parameter["name"]) for parameter in parameters]
    assert len(identities) == len(set(identities))
    assert {
        parameter["name"] for parameter in parameters if parameter["in"] == "query"
    } == expected_queries[operation]
    assert {
        parameter["name"] for parameter in parameters if parameter["in"] == "header"
    } == {"X-Request-ID"}
    assert {
        parameter["name"] for parameter in parameters if parameter["required"]
    } == {
        "search_players": set(),
        "get_player": {"player_id"},
        "get_player_statistics": set(),
        "rank_players": {"metric"},
        "compare_players": {"player_ids"},
    }[operation]


def test_contract_bounds_and_defaults_match_shared_service(contract):
    parameters = contract["components"]["parameters"]
    for name, minimum, maximum, default in (
        ("Limit", 1, player_service.MAX_PAGE_LIMIT, player_service.DEFAULT_PAGE_LIMIT),
        ("Offset", 0, player_service.MAX_PAGE_OFFSET, 0),
        ("GroupLimit", 1, player_service.MAX_GROUP_LIMIT, player_service.DEFAULT_GROUP_LIMIT),
        ("BirthYearMin", 1, 9999, None),
        ("BirthYearMax", 1, 9999, None),
    ):
        schema = parameters[name]["schema"]
        assert schema["type"] == "integer"
        assert (schema["minimum"], schema["maximum"]) == (minimum, maximum)
        assert schema.get("default") == default
    for name in ("SearchText", "BirthCountry"):
        schema = parameters[name]["schema"]
        assert (schema["minLength"], schema["maxLength"]) == (
            1, player_service.MAX_FILTER_TEXT_LENGTH,
        )
    assert parameters["Bats"]["schema"]["enum"] == ["L", "R", "B"]
    assert parameters["Throws"]["schema"]["enum"] == ["L", "R", "S"]
    assert parameters["Direction"]["schema"]["default"] == "desc"
    assert contract["components"]["schemas"]["PlayerId"]["maxLength"] == (
        player_service.MAX_PLAYER_ID_LENGTH
    )
    ids = parameters["PlayerIds"]
    assert (ids["style"], ids["explode"]) == ("form", False)
    assert (ids["schema"]["minItems"], ids["schema"]["maxItems"]) == (
        2, player_service.MAX_COMPARISON_PLAYERS,
    )
    assert ids["schema"]["uniqueItems"] is True


def test_every_documented_example_and_default_matches_its_schema(
    contract, schema_validator
):
    for node in _objects(contract):
        if "schema" in node and "example" in node:
            schema_validator.evolve(schema=node["schema"]).validate(node["example"])
        if "default" in node:
            schema_validator.evolve(schema=node).validate(node["default"])


@pytest.mark.parametrize("operation", OPERATIONS)
def test_documented_parameter_examples_execute_and_match_response_schema(
    contract, schema_validator, express_client, express_headers, operation
):
    path = OPERATIONS[operation][0]
    query = []
    headers = dict(express_headers)
    for parameter in _parameters(contract, operation):
        name, value = parameter["name"], parameter["example"]
        if parameter["in"] == "path":
            path = path.replace("{" + name + "}", value)
        elif parameter["in"] == "header":
            headers[name] = value
        else:
            if parameter["schema"].get("type") == "array":
                assert (parameter["style"], parameter["explode"]) == ("form", False)
                value = ",".join(value)
            query.append((name, value))
    url = path + ("?" + urlencode(query) if query else "")
    response = express_client.get(url, headers=headers)
    assert response.status_code == 200
    payload = _assert_response(
        response, _response_definition(contract, operation, 200), contract, schema_validator
    )
    if operation == "compare_players":
        ids = contract["components"]["parameters"]["PlayerIds"]["example"]
        assert parse_qsl(urlsplit(url).query) == [("player_ids", ",".join(ids))]
        assert [record["playerId"] for record in payload["data"]] == ids


@pytest.mark.parametrize(
    "operation,query",
    [
        ("search_players", ""),
        ("search_players", "?limit=1&offset=1"),
        ("search_players", "?offset=100000"),
        ("search_players", "?q=no-such-player"),
        ("get_player", ""),
        ("get_player_statistics", "?group_limit=1"),
        ("get_player_statistics", "?q=unknown01"),
        ("get_player_statistics", "?q=no-such-player"),
        *[
            ("rank_players", f"?metric={metric}&direction={direction}")
            for metric in ("height", "weight", "birthYear")
            for direction in ("asc", "desc")
        ],
        ("rank_players", "?metric=height&limit=1&offset=1"),
        ("rank_players", "?metric=height&offset=100000"),
        ("rank_players", "?metric=height&q=unknown01"),
        ("rank_players", "?metric=height&q=no-such-player"),
        ("compare_players", "?player_ids=unknown01,aaronha01"),
        ("compare_players", "?player_ids=aaronto01,unknown01,aaronha01"),
    ],
)
def test_success_envelopes_cover_pages_empty_populations_and_nulls(
    contract, schema_validator, express_client, express_headers, operation, query
):
    path = OPERATIONS[operation][0].replace("{player_id}", "unknown01")
    response = express_client.get(path + query, headers=express_headers)
    assert response.status_code == 200
    _assert_response(
        response, _response_definition(contract, operation, 200), contract, schema_validator
    )


@pytest.mark.parametrize("operation", OPERATIONS)
def test_contract_preserves_fractional_attributes_and_unformatted_source_dates(
    contract, schema_validator, express_client, express_headers, player_db, operation
):
    with closing(sqlite3.connect(player_db)) as conn:
        conn.execute(
            "UPDATE players SET birthYear = ?, height = ?, weight = ?, debut = ? "
            "WHERE playerId = ?",
            (1934.25, 72.5, 180.125, "source-defined date", "aaronha01"),
        )
        conn.commit()
    response = express_client.get(OPERATIONS[operation][1], headers=express_headers)
    assert response.status_code == 200
    _assert_response(
        response, _response_definition(contract, operation, 200), contract, schema_validator
    )


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize(
    "status,code",
    [(400, "invalid_request"), (401, "unauthorized"), (414, "request_too_large")],
)
def test_shared_client_errors_match_contract(
    contract, schema_validator, express_client, express_headers, operation, status, code
):
    url = OPERATIONS[operation][1]
    suffix = "unknown=value" if status != 414 else "q=" + "x" * 4096
    url += ("&" if "?" in url else "?") + suffix
    headers = express_headers if status != 401 else {}
    response = express_client.get(url, headers=headers)
    assert response.status_code == status
    payload = _assert_response(
        response, _response_definition(contract, operation, status), contract, schema_validator
    )
    assert payload["error"]["code"] == code


@pytest.mark.parametrize(
    "operation,query",
    [
        ("search_players", "limit=01"),
        ("search_players", "limit=1&limit=2"),
        ("search_players", "birth_year_min=2000&birth_year_max=1900"),
        ("search_players", "q=+"),
        ("search_players", "q=%00"),
        ("search_players", "q=%FF"),
        ("search_players", "q=%"),
        ("get_player", "limit=1"),
        ("compare_players", "player_ids=aaronha01&player_ids=aaronto01"),
    ],
)
def test_wire_rules_beyond_parameter_schemas_remain_enforced(
    contract, schema_validator, express_client, express_headers, operation, query
):
    path = OPERATIONS[operation][0].replace("{player_id}", "aaronha01")
    response = express_client.get(path + "?" + query, headers=express_headers)
    assert response.status_code == 400
    _assert_response(
        response, _response_definition(contract, operation, 400), contract, schema_validator
    )


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize(
    "error,status,code",
    [
        (sqlite3.OperationalError("private diagnostic"), 503, "storage_unavailable"),
        (PlayerQueryTimeoutError(), 504, "query_timeout"),
        (RuntimeError("private diagnostic"), 500, "internal_error"),
    ],
)
def test_sanitized_server_errors_match_contract(
    contract, schema_validator, express_client, express_headers, monkeypatch,
    operation, error, status, code,
):
    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(PlayerService, operation, fail)
    response = express_client.get(OPERATIONS[operation][1], headers=express_headers)
    assert response.status_code == status
    payload = _assert_response(
        response, _response_definition(contract, operation, status), contract, schema_validator
    )
    assert payload["error"]["code"] == code
    assert "private diagnostic" not in response.get_data(as_text=True)


@pytest.mark.parametrize("operation", OPERATIONS)
def test_real_missing_storage_matches_contract_without_creating_a_database(
    contract, schema_validator, express_client, express_headers, monkeypatch, tmp_path,
    operation,
):
    missing = tmp_path / "absent.db"
    monkeypatch.setattr(player_service, "PLAYER_DB", missing)
    response = express_client.get(OPERATIONS[operation][1], headers=express_headers)
    assert response.status_code == 503
    _assert_response(
        response, _response_definition(contract, operation, 503), contract, schema_validator
    )
    assert not missing.exists()


def test_real_locked_storage_matches_contract_and_recovers(
    contract, schema_validator, express_app, express_headers, monkeypatch, player_db
):
    import app as app_module

    monkeypatch.setenv("PLAYER_DB_BUSY_TIMEOUT_SECONDS", "0")
    application = app_module.create_app()
    application.config["TESTING"] = True
    with application.test_client() as client, closing(sqlite3.connect(player_db)) as writer:
        writer.execute("BEGIN EXCLUSIVE")
        response = client.get(OPERATIONS["search_players"][1], headers=express_headers)
        assert response.status_code == 503
        _assert_response(
            response, _response_definition(contract, "search_players", 503),
            contract, schema_validator,
        )
        writer.rollback()
        response = client.get(OPERATIONS["search_players"][1], headers=express_headers)
        assert response.status_code == 200
        _assert_response(
            response, _response_definition(contract, "search_players", 200),
            contract, schema_validator,
        )


@pytest.mark.parametrize(
    "operation,url",
    [
        ("get_player", API_PREFIX + "/players/missing01"),
        ("compare_players", API_PREFIX + "/comparisons?player_ids=aaronha01,missing01"),
    ],
)
def test_missing_players_match_the_documented_404(
    contract, schema_validator, express_client, express_headers, operation, url
):
    response = express_client.get(url, headers=express_headers)
    assert response.status_code == 404
    payload = _assert_response(
        response, _response_definition(contract, operation, 404), contract, schema_validator
    )
    assert payload["error"]["code"] == "player_not_found"


@pytest.mark.parametrize(
    "method,url,status,component,code",
    [
        ("GET", API_PREFIX + "/unknown", 404, "NotFound", "not_found"),
        ("GET", API_PREFIX + "/players/a%2Fb", 404, "NotFound", "not_found"),
        ("POST", API_PREFIX + "/players", 405, "MethodNotAllowed", "method_not_allowed"),
    ],
)
def test_namespace_routing_errors_match_shared_response_components(
    contract, schema_validator, express_client, express_headers,
    method, url, status, component, code,
):
    response = express_client.open(url, method=method, headers=express_headers)
    assert response.status_code == status
    payload = _assert_response(
        response, contract["components"]["responses"][component], contract, schema_validator
    )
    assert payload["error"]["code"] == code
    if status == 405:
        assert {method.strip() for method in response.headers["Allow"].split(",")} == {
            "GET", "HEAD", "OPTIONS",
        }


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("method", ["HEAD", "OPTIONS"])
def test_automatic_methods_share_headers_but_not_get_response_bodies(
    contract, schema_validator, express_client, express_headers, operation, method
):
    response = express_client.open(
        OPERATIONS[operation][1], method=method, headers=express_headers
    )
    assert response.status_code == 200
    assert response.data == b""
    _assert_headers(
        response, _response_definition(contract, operation, 200), contract, schema_validator
    )


@pytest.mark.parametrize("operation", OPERATIONS)
def test_response_schemas_reject_missing_extra_and_malformed_envelope_fields(
    contract, schema_validator, express_client, express_headers, operation
):
    response = express_client.get(OPERATIONS[operation][1], headers=express_headers)
    assert response.status_code == 200
    payload = response.get_json()
    definition = _response_definition(contract, operation, 200)
    validator = schema_validator.evolve(
        schema=definition["content"]["application/json"]["schema"]
    )
    assert validator.is_valid(payload)
    for field in payload:
        incomplete = dict(payload)
        del incomplete[field]
        assert not validator.is_valid(incomplete), field
    assert not validator.is_valid({**payload, "unexpected": True})
    assert not validator.is_valid({**payload, "request_id": "id\n"})


@pytest.mark.parametrize(
    "operation,path,value",
    [
        ("search_players", ("data",), {}),
        ("search_players", ("pagination", "next_offset"), 100001),
        ("get_player", ("data",), []),
        ("get_player", ("data", "height"), "72"),
        ("get_player_statistics", ("numeric", "height", "mean"), "unknown"),
        ("get_player_statistics", ("groups", "bats", "other_count"), -1),
        ("rank_players", ("metric",), "homeRuns"),
        ("rank_players", ("excluded_count",), -1),
        ("compare_players", ("data",), []),
        ("compare_players", ("differences",), []),
        ("compare_players", ("differences", 0, "numeric", "height"), "unknown"),
    ],
)
def test_nested_response_shapes_and_bounds_are_not_permissive(
    contract, schema_validator, express_client, express_headers, operation, path, value
):
    response = express_client.get(OPERATIONS[operation][1], headers=express_headers)
    assert response.status_code == 200
    payload = deepcopy(response.get_json())
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    definition = _response_definition(contract, operation, 200)
    assert not schema_validator.evolve(
        schema=definition["content"]["application/json"]["schema"]
    ).is_valid(payload)


@pytest.mark.parametrize(
    "value,valid",
    [
        ("a", True), ("Ab09" * 8, True), ("a" * 33, False),
        ("", False), ("a\n", False), ("bad-id", False), ("quote'01", False),
        ("\u00e9", False),
    ],
)
def test_lookup_id_schema_is_stricter_than_stored_player_ids(
    contract, schema_validator, player_records, value, valid
):
    validator = schema_validator.evolve(schema=contract["components"]["schemas"]["PlayerId"])
    assert validator.is_valid(value) is valid
    schema_validator.evolve(schema=contract["components"]["schemas"]["Player"]).validate(
        player_records[2]
    )


def test_every_seed_search_page_matches_the_contract(
    contract, schema_validator, express_client, express_headers, seed_player_records
):
    offset = 0
    ids = []
    while offset is not None:
        response = express_client.get(
            API_PREFIX + "/players",
            query_string={"limit": 100, "offset": offset},
            headers={**express_headers, "X-Request-ID": f"contract-seed-{offset}"},
        )
        assert response.status_code == 200
        payload = _assert_response(
            response, _response_definition(contract, "search_players", 200),
            contract, schema_validator,
        )
        assert payload["pagination"]["total"] == len(seed_player_records)
        ids.extend(record["playerId"] for record in payload["data"])
        next_offset = payload["pagination"]["next_offset"]
        assert next_offset is None or next_offset > offset
        offset = next_offset
    assert len(ids) == len(set(ids)) == len(seed_player_records) == 19370
    assert set(ids) == {record["playerId"] for record in seed_player_records}


def test_seed_analytics_and_maximum_comparison_match_the_contract(
    contract, schema_validator, express_client, express_headers, seed_player_records
):
    nullable_id = next(
        record["playerId"] for record in seed_player_records if record["height"] is None
    )
    ids = [nullable_id] + [
        record["playerId"] for record in seed_player_records
        if record["playerId"] != nullable_id
    ][:4]
    requests = [
        ("get_player", API_PREFIX + "/players/" + nullable_id),
        ("get_player_statistics", API_PREFIX + "/statistics?group_limit=100"),
        ("rank_players", API_PREFIX + "/rankings?metric=height&limit=100"),
        ("compare_players", API_PREFIX + "/comparisons?player_ids=" + ",".join(ids)),
    ]
    for operation, url in requests:
        response = express_client.get(url, headers=express_headers)
        assert response.status_code == 200
        payload = _assert_response(
            response, _response_definition(contract, operation, 200),
            contract, schema_validator,
        )
        if operation == "compare_players":
            assert len(payload["data"]) == 5
            assert len(payload["differences"]) == 10
            assert any(pair["numeric"]["height"] is None for pair in payload["differences"])
