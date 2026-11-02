from urllib.parse import quote

import pytest


def test_get_players_returns_data(client, player_records):
    response = client.get("/v1/players")
    assert response.status_code == 200
    players = response.get_json()
    assert isinstance(players, list)
    assert len(players) > 0
    assert "playerId" in players[0]
    assert players == player_records


@pytest.mark.parametrize("record_index", [0, 2, 3])
def test_get_player_preserves_detail_response(client, player_records, record_index):
    record = player_records[record_index]
    response = client.get(f"/v1/players/{quote(record['playerId'], safe='')}")

    assert response.status_code == 200
    assert response.get_json() == record


@pytest.mark.parametrize(
    "player_id",
    ["missing", "aaronha01' OR 1=1 --", "aaronha01'; DROP TABLE players; --"],
)
def test_missing_player_preserves_intended_legacy_error(client, player_id):
    response = client.get(f"/v1/players/{quote(player_id, safe='')}")

    assert response.status_code == 200
    assert response.get_json() == {"error": f"No record found with player_id={player_id}"}
