from flask import Flask

from app.routes.items import items_bp
from app.routes.playback_commands import playback_bp


def create_app():
    app = Flask(__name__)
    app.register_blueprint(items_bp)
    app.register_blueprint(playback_bp)
    return app