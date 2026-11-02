import ollama
import pandas as pd
from flask import Flask, jsonify
from sqlalchemy import create_engine

from app_paths import PLAYER_CSV, PLAYER_DB
from express_api import configure_express_api
from player_service import PlayerService


def ensure_player_db() -> None:
    if PLAYER_DB.exists():
        return
    df = pd.read_csv(PLAYER_CSV)
    engine = create_engine(f"sqlite:///{PLAYER_DB}", echo=False)
    df.to_sql("players", con=engine, if_exists="replace", index=False)


def get_players():
    player_service = PlayerService()
    result = player_service.get_all_players()
    return result


def query_player_id(player_id):
    player_service = PlayerService()
    result = player_service.search_by_player(player_id)

    if len(result) == 0:
        return jsonify({"error": "No record found with player_id={}".format(player_id)})
    else:
        return jsonify(result)


def list_models():
    try:
        return jsonify(ollama.list())
    except Exception as exc:
        return jsonify(
            {
                "error": "Ollama is unavailable. Start the Ollama container and pull tinyllama.",
                "detail": str(exc),
            }
        ), 503


def chat():
    try:
        response = ollama.chat(
            model="tinyllama",
            messages=[
                {
                    "role": "user",
                    "content": "Why is the sky blue?",
                },
            ],
        )
        return jsonify(response), 200
    except Exception as exc:
        return jsonify(
            {
                "error": "Ollama chat failed. Ensure tinyllama is pulled and Ollama is running on port 11434.",
                "detail": str(exc),
            }
        ), 503


def create_app() -> Flask:
    application = Flask(__name__)
    configure_express_api(application)
    ensure_player_db()
    application.add_url_rule("/v1/players", view_func=get_players, methods=["GET"])
    application.add_url_rule("/v1/players/<string:player_id>", view_func=query_player_id)
    application.add_url_rule("/v1/chat/list-models", view_func=list_models)
    application.add_url_rule("/v1/chat", view_func=chat, methods=["POST"])
    return application


app = create_app()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
