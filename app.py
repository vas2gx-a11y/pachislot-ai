import os

from flask import Flask

import common
from routes.records import records_bp
from routes.machines import machines_bp
from routes.expected_value import expected_value_bp

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))
app.config["MAX_CONTENT_LENGTH"] = common.MAX_UPLOAD_SIZE

app.register_blueprint(records_bp)
app.register_blueprint(machines_bp)
app.register_blueprint(expected_value_bp)

# テンプレート側でスコア内訳を組み立てるために、common.py の変換関数を
# Jinjaのグローバル関数として登録しておく(ロジックの二重管理を避けるため)
app.jinja_env.globals["describe_category_scores"] = common.describe_category_scores
app.jinja_env.globals["category_scores_total"] = common.category_scores_total


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=debug_mode)
