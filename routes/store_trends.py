import base64

from flask import Blueprint, render_template, request, redirect, url_for, flash

import common

store_trends_bp = Blueprint("store_trends", __name__, url_prefix="/store_trends")

# 集計対象期間の選択肢(ラベル, 日数)。0は全期間。
PERIOD_CHOICES = [("直近30日", 30), ("直近90日", 90), ("直近1年", 365), ("全期間", 0)]
DEFAULT_DAYS = 90


def _parse_days(raw):
    """期間パラメータを検証する(想定外の値が来たら既定値に戻す)"""
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return days if days in {d for _, d in PERIOD_CHOICES} else DEFAULT_DAYS


def _render(store_name, days, ai_summary="", import_preview=None, pasted_text=""):
    stores = common.list_store_names()

    # 店舗が未指定なら、記録が一番多い店舗を初期表示にする(毎回選び直す手間を省く)
    if not store_name and stores:
        store_name = stores[0]["name"]

    trends = common.build_store_trends(store_name, days=days) if store_name else None
    store_stats = common.load_store_stats().get(store_name) if store_name else None
    # 取り込んだホールデータ(店の全台)は、自分の記録より対象期間が長いことが多いので
    # 画面の期間指定とは別に、常に直近1年分を集計する
    daily_trends = common.build_store_daily_trends(store_name, days=365) if store_name else None

    return render_template(
        "store_trends.html",
        stores=stores,
        store_name=store_name,
        days=days,
        period_choices=PERIOD_CHOICES,
        trends=trends,
        store_stats=store_stats,
        daily_trends=daily_trends,
        import_preview=import_preview,
        pasted_text=pasted_text,
        ai_summary=ai_summary,
    )


def _back_to(store_name, days):
    return redirect(url_for("store_trends.index", store_name=store_name, days=days))


@store_trends_bp.route("/")
def index():
    store_name = request.args.get("store_name", "").strip()
    days = _parse_days(request.args.get("days", DEFAULT_DAYS))
    return _render(store_name, days)


@store_trends_bp.route("/ai_summary", methods=["POST"])
def ai_summary():
    """集計結果をAIに総評してもらう(ボタンを押したときだけAPIを呼ぶ)"""
    store_name = request.form.get("store_name", "").strip()
    days = _parse_days(request.form.get("days", DEFAULT_DAYS))

    trends = common.build_store_trends(store_name, days=days) if store_name else None
    if not trends or not trends.get("record_count"):
        flash("この条件では集計できる記録がないため、AI総評を作成できません。")
        return _render(store_name, days)

    store_stats = common.load_store_stats().get(store_name)
    daily_trends = common.build_store_daily_trends(store_name, days=365)
    summary, error = common.summarize_store_trends_with_gemini(
        trends, store_stats=store_stats, daily_trends=daily_trends
    )
    if error:
        flash(error)
    return _render(store_name, days, ai_summary=summary)


@store_trends_bp.route("/upload_stats", methods=["POST"])
def upload_stats():
    """
    データサイト等のスクショから、店舗の年間データ(総差枚・平均差枚・平均G数・勝率)を
    AIに読み取らせて保存する。

    どの店舗のデータとして保存するかは、画面で選択中の店舗名を正とする。
    (画像から読み取った店名は表記ゆれが起きやすく、記録側の店舗名と一致しないと
    分析で紐づかないため。画像の店名が違って見える場合は警告だけ出す)
    """
    store_name = request.form.get("store_name", "").strip()
    days = _parse_days(request.form.get("days", DEFAULT_DAYS))
    file = request.files.get("stats_image")

    if not file or file.filename == "":
        flash("年間データの画像を選択してください。")
        return _back_to(store_name, days)

    if not common.allowed_file(file.filename):
        flash("対応していないファイル形式です(jpg / jpeg / png / webp のみ)")
        return _back_to(store_name, days)

    ext = file.filename.rsplit(".", 1)[1].lower()
    mime_type = "image/png" if ext == "png" else "image/webp" if ext == "webp" else "image/jpeg"
    base64_image = base64.b64encode(file.read()).decode("utf-8")

    parsed = common.analyze_store_stats_image_with_gemini(base64_image, mime_type)
    if not parsed:
        flash("画像の解析に失敗しました。もう一度お試しいただくか、下の入力欄に手入力してください。")
        return _back_to(store_name, days)

    # 画面で店舗が選ばれていない場合に限り、画像から読み取った店名を使う
    target_store = store_name or parsed.get("store_name", "").strip()
    if not target_store:
        flash("保存先の店舗が特定できませんでした。店舗を選んでから、もう一度お試しください。")
        return _back_to(store_name, days)

    ai_store_name = parsed.get("store_name", "").strip()
    if ai_store_name and store_name and ai_store_name != store_name:
        flash(f"画像から読み取った店名「{ai_store_name}」は選択中の「{store_name}」と異なります。"
              f"選択中の店舗のデータとして保存しました。違う場合は店舗を選び直してください。")

    read_values = [k for k in ("total_diff", "avg_diff", "avg_games", "win_rate") if parsed.get(k) is not None]
    if not read_values:
        flash("画像から数値を読み取れませんでした。下の入力欄に手入力してください。")
        return _back_to(target_store, days)

    ok, message = common.save_store_stats(
        target_store,
        period_label=parsed.get("period_label", ""),
        total_diff=parsed.get("total_diff"),
        avg_diff=parsed.get("avg_diff"),
        avg_games=parsed.get("avg_games"),
        win_rate=parsed.get("win_rate"),
        note=parsed.get("note", ""),
        source="画像から読み取り",
    )
    flash(message if ok else message)
    if ok and len(read_values) < 4:
        missing = {"total_diff": "総差枚", "avg_diff": "平均差枚", "avg_games": "平均G数", "win_rate": "勝率"}
        not_read = [label for key, label in missing.items() if key not in read_values]
        flash(f"画像から読み取れなかった項目({'、'.join(not_read)})があります。下の入力欄で補ってください。")
    return _back_to(target_store, days)


@store_trends_bp.route("/save_stats", methods=["POST"])
def save_stats():
    """AIの読み取り結果の修正・手入力での保存"""
    store_name = request.form.get("store_name", "").strip()
    days = _parse_days(request.form.get("days", DEFAULT_DAYS))

    if not store_name:
        flash("店舗を選んでから保存してください。")
        return _back_to(store_name, days)

    ok, message = common.save_store_stats(
        store_name,
        period_label=request.form.get("period_label", ""),
        total_diff=common.parse_number(request.form.get("total_diff")),
        avg_diff=common.parse_number(request.form.get("avg_diff")),
        avg_games=common.parse_number(request.form.get("avg_games")),
        win_rate=common.parse_number(request.form.get("win_rate")),
        note=request.form.get("note", ""),
        source="手入力",
    )
    flash(message)
    return _back_to(store_name, days)


@store_trends_bp.route("/import_daily", methods=["POST"])
def import_daily():
    """
    ホールデータサイトの一覧表をコピーして貼り付けたテキストを解析し、
    店舗の日別データとして一括登録する。

    いきなり保存すると、貼り付け形式が想定と違ったときに変な値がシートに入ってしまうため、
    1回目は解析結果のプレビューを返し、内容を確認してから保存する2段階にしている。
    """
    store_name = request.form.get("store_name", "").strip()
    days = _parse_days(request.form.get("days", DEFAULT_DAYS))
    pasted_text = request.form.get("pasted_text", "")
    confirmed = request.form.get("confirm") == "1"

    if not store_name:
        flash("取り込み先の店舗を選んでください。")
        return _render(store_name, days)

    if not pasted_text.strip():
        flash("ホールデータの表をコピーして貼り付けてください。")
        return _render(store_name, days)

    rows, report = common.parse_hall_daily_text(pasted_text)
    if not rows:
        flash("貼り付けたテキストから日別データを読み取れませんでした。"
              "日付・総差枚・平均差枚・平均G数・勝率が並んだ表をそのままコピーしてください。")
        return _render(store_name, days, pasted_text=pasted_text)

    if not confirmed:
        # プレビュー(まだ保存しない)
        return _render(store_name, days, pasted_text=pasted_text,
                       import_preview={"rows": rows[:10], "report": report, "total": len(rows)})

    ok, message, _counts = common.save_store_daily_rows(store_name, rows)
    flash(message)
    if ok and report["skipped"]:
        flash(f"列が足りずに読み飛ばした行が{report['skipped']}件あります。"
              f"表の一部だけをコピーしていないか確認してください。")
    return _back_to(store_name, days)
