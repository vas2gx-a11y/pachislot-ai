from flask import Blueprint, flash, g, redirect, render_template, request, session, url_for

import common

auth_bp = Blueprint("auth", __name__)


def _safe_next(target):
    """ログイン後の戻り先。外部サイトへ飛ばされないよう、このアプリ内のパスだけを許す。"""
    target = (target or "").strip()
    if target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("records.index")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(_safe_next(request.args.get("next")))

    login_id = ""
    if request.method == "POST":
        login_id = request.form.get("login_id", "").strip()
        user, error = common.authenticate(login_id, request.form.get("password", ""))
        if error:
            flash(error)
        else:
            # セッション固定攻撃を避けるため、ログインのたびに中身を作り直す
            session.clear()
            session["user_id"] = user["user_id"]
            # スマホで打ちながら使うので、毎回ログインし直さなくていいよう長めに保持する
            session.permanent = True
            return redirect(_safe_next(request.form.get("next")))

    return render_template("login.html", login_id=login_id, next_path=request.values.get("next", ""))


@auth_bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


@auth_bp.route("/account", methods=["GET", "POST"])
def account():
    """自分のパスワード変更。管理者が仮のパスワードで作ったメンバーが、自分で変えられるように。"""
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        if new != request.form.get("new_password_confirm", ""):
            flash("新しいパスワードが確認用と一致しません。")
        elif not common.authenticate(g.user["login_id"], current)[0]:
            flash("今のパスワードが違います。")
        else:
            ok, message = common.set_user_password(g.user["user_id"], new)
            flash(message)
            if ok:
                return redirect(url_for("auth.account"))
    return render_template("account.html")
