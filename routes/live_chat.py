"""
実戦チャット。打ちながら状況を送り、ヤメ時や設定をAIと相談する画面。

機種の知識は machine_data/<id>.json(機種情報)を渡す。サーバーは会話を持たず、
履歴はブラウザが毎回送ってくる(理由は common.py の「実戦チャット」セクション)。
"""

from flask import Blueprint, abort, jsonify, render_template, request

import common
import machine_info

live_chat_bp = Blueprint("live_chat", __name__, url_prefix="/chat")


@live_chat_bp.route("/")
def index():
    machines, errors = machine_info.load_all()
    return render_template(
        "live_chat.html",
        # 判別ログは機種名で引くので、名前も渡しておく
        machines=[{"id": m.get("id"), "name": m.get("name")} for m in machines],
        errors=errors,
        preselect_id=request.args.get("machine_id", ""),
    )


@live_chat_bp.route("/api", methods=("POST",))
def api():
    data = request.get_json(silent=True) or {}
    m = machine_info.load(str(data.get("machine_id") or ""))
    if m is None:
        abort(404)

    history = data.get("history")
    reply, is_error = common.live_chat_reply(
        m,
        history if isinstance(history, list) else [],
        data.get("message"),
        str(data.get("judge_note") or "")[:1000],
    )
    return jsonify({"reply": reply, "error": is_error})
