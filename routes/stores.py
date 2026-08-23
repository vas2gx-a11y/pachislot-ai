from flask import Blueprint, render_template, request, redirect, url_for, flash

import common

stores_bp = Blueprint("stores", __name__, url_prefix="/stores")


def _back(store_name=""):
    return redirect(url_for("stores.index", highlight=store_name))


@stores_bp.route("/")
def index():
    """
    店舗の管理画面。

    店舗名は専用のマスタを持たず「どのシートに登場するか」で決まるため、
    どの店舗にどんなデータが入っているかが分かりにくい。
    ここで一覧にして、追加・名前変更・統合をまとめて行えるようにする。
    """
    overview = common.build_store_overview()
    return render_template(
        "stores.html",
        overview=overview,
        highlight=request.args.get("highlight", "").strip(),
    )


@stores_bp.route("/add", methods=["POST"])
def add():
    """
    店舗を新しく登録する。

    まだデータが1件も無い状態でも一覧に残るよう、旧イベント日のシートに
    店名だけの行を作っておく(そうしないと画面を離れた時点で消えてしまう)。
    """
    store_name = request.form.get("store_name", "").strip()
    if not store_name:
        flash("追加する店舗名を入力してください。")
        return _back()

    if any(s["name"] == store_name for s in common.build_store_overview()):
        flash(f"「{store_name}」はすでに登録されています。")
        return _back(store_name)

    ok, message = common.save_store_events(store_name, event_days="", anniversary_days="",
                                           note="", source="店舗追加")
    flash(f"店舗「{store_name}」を追加しました。" if ok else message)
    return _back(store_name)


@stores_bp.route("/rename", methods=["POST"])
def rename():
    """店舗名を変更する(記録・年間データ・旧イベント日・日別・台別のすべてに反映)"""
    old_name = request.form.get("old_name", "").strip()
    new_name = request.form.get("new_name", "").strip()

    ok, message = common.rename_store(old_name, new_name)
    flash(message)
    return _back(new_name if ok else old_name)


@stores_bp.route("/merge", methods=["POST"])
def merge():
    """
    表記ゆれで分かれてしまった店舗を1つにまとめる。

    中身は名前変更と同じ処理(統合元を統合先の名前に書き換える)。
    やっていることは同じでも、画面上は「統合」と「名前の変更」で
    目的が違うので入口を分けている。
    """
    source = request.form.get("source_name", "").strip()
    target = request.form.get("target_name", "").strip()

    if source and source == target:
        flash("統合元と統合先が同じ店舗です。")
        return _back(target)

    ok, message = common.rename_store(source, target)
    if ok:
        message = f"「{source}」を「{target}」に統合しました。" + message.split("に変更しました", 1)[-1]
    flash(message)
    return _back(target if ok else source)
