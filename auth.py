"""
ログインの強制と、管理者だけのページの制限。

ユーザーのデータ(users シート)の読み書きは common.py の「ユーザー」セクションにあり、
ここは「誰が今どのページを開いてよいか」の判定だけを持つ。
全ページを before_request でまとめて守るのは、ページを足したときに
ログインのチェックを付け忘れても、既定で閉じた状態になるようにするため。
"""
from functools import wraps

from flask import abort, g, jsonify, redirect, request, session, url_for

import common

# ログインしていなくても開けるエンドポイント
PUBLIC_ENDPOINTS = {"auth.login", "static"}


def load_logged_in_user():
    """
    セッションのユーザーを g.user に入れ、未ログインならログイン画面へ回す(before_request)。

    セッションにはユーザーIDだけを持ち、毎回 users シート(キャッシュ)から引き直す。
    停止したメンバーを、セッションの期限切れを待たずに締め出すため。
    """
    g.user = None
    user = common.find_user(session.get("user_id"))
    if user and user["active"]:
        g.user = user
    elif "user_id" in session:
        session.clear()

    if g.user or request.endpoint in PUBLIC_ENDPOINTS:
        return None
    # 画面の裏で呼ぶAPI(fetch)にログイン画面のHTMLを返しても処理できないので、401で返す
    if "/api" in request.path:
        return jsonify({"ok": False, "error": "ログインが切れました。ページを読み込み直してください。"}), 401
    next_path = request.full_path if request.method == "GET" else ""
    return redirect(url_for("auth.login", next=next_path.rstrip("?") or None))


def admin_required(view):
    """管理者だけが開けるページにする(メンバーには403)。"""
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not common.is_admin():
            abort(403)
        return view(*args, **kwargs)
    return wrapper


def require_admin_for_blueprint():
    """Blueprint の before_request に登録して、配下を丸ごと管理者だけにする。"""
    if not common.is_admin():
        abort(403)
