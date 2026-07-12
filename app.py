import os

from flask import Flask

import common
from routes.records import records_bp
from routes.machines import machines_bp

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))
app.config["MAX_CONTENT_LENGTH"] = common.MAX_UPLOAD_SIZE

app.register_blueprint(records_bp)
app.register_blueprint(machines_bp)


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=debug_mode)
