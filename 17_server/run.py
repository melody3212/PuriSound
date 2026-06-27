import os

from dotenv import load_dotenv

from app import create_app

load_dotenv()

app = create_app()


@app.route("/")
def health():
    return {
        "status": "ok",
        "message": "Flask CRUD API",
        "endpoints": {
            "list": "GET /api/items",
            "get": "GET /api/items/<id>",
            "create": "POST /api/items",
            "update": "PUT /api/items/<id>",
            "delete": "DELETE /api/items/<id>",
            "playback_list": "GET /api/playback-commands",
            "playback_latest": "GET /api/playback-commands/latest",
            "playback_create": "POST /api/playback-commands",
        },
    }


if __name__ == "__main__":
    port = int(os.getenv("FLASK_PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)