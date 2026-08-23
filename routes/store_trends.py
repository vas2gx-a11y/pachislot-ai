from flask import Blueprint, render_template, request, flash

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


def _render(store_name, days, ai_summary=""):
    stores = common.list_store_names()

    # 店舗が未指定なら、記録が一番多い店舗を初期表示にする(毎回選び直す手間を省く)
    if not store_name and stores:
        store_name = stores[0]["name"]

    trends = common.build_store_trends(store_name, days=days) if store_name else None

    return render_template(
        "store_trends.html",
        stores=stores,
        store_name=store_name,
        days=days,
        period_choices=PERIOD_CHOICES,
        trends=trends,
        ai_summary=ai_summary,
    )


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

    summary, error = common.summarize_store_trends_with_gemini(trends)
    if error:
        flash(error)
    return _render(store_name, days, ai_summary=summary)
