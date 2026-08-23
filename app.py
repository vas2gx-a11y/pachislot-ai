import gzip
import os

from flask import Flask, request, url_for

import common
import navigation
from routes.records import records_bp
from routes.machines import machines_bp
from routes.expected_value import expected_value_bp
from routes.store_trends import store_trends_bp
from routes.stores import stores_bp
from routes.calendar import calendar_bp
from routes.nav import nav_bp

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))
app.config["MAX_CONTENT_LENGTH"] = common.MAX_UPLOAD_SIZE

app.register_blueprint(records_bp)
app.register_blueprint(machines_bp)
app.register_blueprint(expected_value_bp)
app.register_blueprint(store_trends_bp)
app.register_blueprint(stores_bp)
app.register_blueprint(calendar_bp)
app.register_blueprint(nav_bp)

# テンプレート側でスコア内訳を組み立てるために、common.py の変換関数を
# Jinjaのグローバル関数として登録しておく(ロジックの二重管理を避けるため)
app.jinja_env.globals["describe_category_scores"] = common.describe_category_scores
app.jinja_env.globals["category_scores_total"] = common.category_scores_total


# ---------------------------------------------------------------------------
# レスポンスのgzip圧縮
# ---------------------------------------------------------------------------
# 店舗の傾向ページのように表が多いページはHTMLが1MB近くになる。中身はクラス名の
# 繰り返しがほとんどなので、gzipすれば数十KBまで縮む(実測 1052KB → 45KB)。
# 回線の細いスマホでの読み込み時間に直結するため、テキスト系のレスポンスは圧縮して返す。
# 小さいレスポンスは圧縮しても得しない(むしろCPUの無駄)ので下限を設けている。
GZIP_MIN_SIZE = 1024
GZIP_LEVEL = 6


@app.after_request
def compress_response(response):
    if "gzip" not in request.headers.get("Accept-Encoding", "").lower():
        return response
    # ファイル送信などのストリーミング応答は get_data() すると壊れるので触らない
    if response.direct_passthrough or response.headers.get("Content-Encoding"):
        return response
    if not (200 <= response.status_code < 300):
        return response

    content_type = (response.content_type or "").split(";")[0].strip()
    if not (content_type.startswith("text/")
            or content_type in {"application/json", "application/javascript", "image/svg+xml"}):
        return response

    data = response.get_data()
    if len(data) < GZIP_MIN_SIZE:
        return response

    response.set_data(gzip.compress(data, GZIP_LEVEL))
    response.headers["Content-Encoding"] = "gzip"
    response.headers["Content-Length"] = response.content_length
    response.headers.add("Vary", "Accept-Encoding")
    return response


@app.before_request
def handle_cache_refresh():
    """
    ?refresh=1 が付いていたらキャッシュを捨てて、シートから読み直させる。

    アプリから保存したデータはその場でキャッシュを無効化しているので通常は不要だが、
    スプレッドシートを直接編集したときの反映待ち(最大TTLぶん)を飛ばすために使う。
    """
    if request.args.get("refresh"):
        common.refresh_caches()


def _endpoint_path(endpoint):
    """エンドポイント名からURLを引く(引数が必要なページは前方一致の判定用に使う)"""
    try:
        return url_for(endpoint)
    except Exception:
        return None


def _active_keys():
    """
    今どのカテゴリ・どの機能を見ているかを判定する。

    URLの前方一致で見るのは、機能ページ配下にサブページ(例: /store_trends/my_records)が
    増えても選択状態が外れないようにするため。
    一致が複数ある場合は、より長く一致した方(=より具体的なURL)を採用する。
    """
    path = request.path
    best = (None, None, -1)
    for category in navigation.NAV:
        for item in category["items"]:
            base = item.get("match") or _endpoint_path(item["endpoint"])
            if not base:
                continue
            if path == base or path.startswith(base if base.endswith("/") else base + "/"):
                if len(base) > best[2]:
                    best = (category["key"], item["endpoint"], len(base))

    # カテゴリのトップページ(/c/<key>)を開いている場合
    if path.startswith("/c/"):
        return path[len("/c/"):].strip("/"), None
    return best[0], best[1]


@app.context_processor
def inject_navigation():
    """全テンプレートでナビ構成と現在地を使えるようにする"""
    active_category, active_item = _active_keys()
    return {
        "nav_categories": navigation.NAV,
        "nav_visible_items": navigation.visible_items,
        "nav_mobile_primary": navigation.mobile_primary(),
        "nav_mobile_overflow": navigation.mobile_overflow(),
        "nav_active_category": active_category,
        "nav_active_item": active_item,
    }


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=debug_mode)
