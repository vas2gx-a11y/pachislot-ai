"""
実戦チャット。打ちながら状況を送り、ヤメ時や設定をAIと相談する画面。

機種の知識は machine_data/<id>.json(機種情報)を渡す。サーバーは会話を持たず、
履歴はブラウザが毎回送ってくる(理由は common.py の「実戦チャット」セクション)。
終了時だけ、確認済みの記録と台メモをシートに保存する。
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
        store_names=[s["name"] for s in common.list_store_names()],
    )


@live_chat_bp.route("/api", methods=("POST",))
def api():
    data = request.get_json(silent=True) or {}
    m = _load_machine(data)

    image = data.get("image") if isinstance(data.get("image"), dict) else {}
    image_part, image_error = common.live_chat_image_part(image.get("mime_type"), image.get("data"))
    if image_error:
        return jsonify({"reply": image_error, "error": True})

    reply, is_error = common.live_chat_reply(
        m,
        _history(data),
        data.get("message"),
        str(data.get("judge_note") or "")[:1000],
        image_part,
        common.unit_notes_for(data.get("store_name"), data.get("machine_number")),
    )
    return jsonify({"reply": reply, "error": is_error})


def _load_machine(data):
    m = machine_info.load(str(data.get("machine_id") or ""))
    if m is None:
        abort(404)
    return m


def _history(data):
    history = data.get("history")
    return history if isinstance(history, list) else []


@live_chat_bp.route("/api/unit_notes")
def unit_notes():
    """店舗と台番号を入れた時点で、その台の過去メモを画面に出すため。"""
    notes = common.unit_notes_for(request.args.get("store_name"), request.args.get("machine_number"))
    return jsonify({"notes": [
        {"date": str(n.get("date", "")), "machine_name": n.get("machine_name", ""), "note": n.get("note", "")}
        for n in notes
    ]})


@live_chat_bp.route("/api/summarize", methods=("POST",))
def summarize():
    data = request.get_json(silent=True) or {}
    m = _load_machine(data)
    draft, error = common.live_chat_summarize(
        m, _history(data), str(data.get("session_id") or ""),
        data.get("store_name"), data.get("machine_number"),
    )
    return jsonify({"draft": draft, "error": error})


@live_chat_bp.route("/api/save", methods=("POST",))
def save():
    data = request.get_json(silent=True) or {}
    ok, message = common.save_live_chat_result(_load_machine(data), data)
    return jsonify({"ok": ok, "message": message})
