from flask import Blueprint, flash, redirect, render_template, request, url_for

import auth
import common

members_bp = Blueprint("members", __name__, url_prefix="/members")
members_bp.before_request(auth.require_admin_for_blueprint)


@members_bp.route("/")
def index():
    # 使いすぎている人がすぐ分かるよう、一覧にも今月の使用料を並べる
    usage = {r["user_id"]: r for r in common.build_api_usage_report()["rows"]}
    return render_template("members.html", users=common.load_users(), usage=usage)


@members_bp.route("/add", methods=["POST"])
def add():
    """
    メンバーを追加する。パスワードは管理者が仮に決めて本人に伝え、本人がアカウント画面で変える。
    招待メールの仕組みを持たないのは、メール送信の設定を増やさずに済ませるため。
    """
    ok, message = common.create_user(
        request.form.get("login_id", ""),
        request.form.get("display_name", ""),
        request.form.get("password", ""),
    )
    flash(message)
    return redirect(url_for("members.index"))


@members_bp.route("/<user_id>/password", methods=["POST"])
def reset_password(user_id):
    """パスワードを忘れたメンバーのために、管理者が仮のパスワードを設定し直す"""
    ok, message = common.set_user_password(user_id, request.form.get("password", ""))
    flash(message)
    return redirect(url_for("members.index"))


@members_bp.route("/<user_id>/active", methods=["POST"])
def set_active(user_id):
    ok, message = common.set_user_active(user_id, request.form.get("active") == "1")
    flash(message)
    return redirect(url_for("members.index"))


@members_bp.route("/usage")
def usage():
    """アカウントごとのAI(Gemini)使用料の概算"""
    return render_template("members_usage.html",
                           report=common.build_api_usage_report(request.args.get("month")))
