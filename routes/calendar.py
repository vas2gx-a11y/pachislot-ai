from datetime import datetime

from flask import Blueprint, render_template, request, url_for

import common

calendar_bp = Blueprint("calendar", __name__, url_prefix="/calendar")


def _selected_stores():
    """
    絞り込み中の店舗を返す(空リスト = 全店舗の総合カレンダー)。

    プルダウンは1店舗だけの選択だが、後から複数選択に広げても
    ページ側を直さずに済むよう、リストで受け渡しする。
    """
    names = [n.strip() for n in request.args.getlist("store_name") if n.strip()]
    # 重複を除きつつ、指定された順番は保つ
    seen = set()
    return [n for n in names if not (n in seen or seen.add(n))]


@calendar_bp.route("/")
def index():
    """
    月別のイベントカレンダー。

    店舗に登録した旧イベント日・周年日のルールをその月の日付に展開して並べ、
    取り込んだホールデータや自分の記録も同じマスに重ねて表示する。
    日付を選ぶと、その日の詳細を下の欄(JSが使えるならポップアップ)に出す。
    """
    year, month = common.normalize_calendar_month(
        request.args.get("year"), request.args.get("month"))
    store_names = _selected_stores()

    calendar_data = common.build_event_calendar(year, month, store_names=store_names)

    # 日付が指定されていればその日の詳細も一緒に描画する。
    # (JSが動かない環境でもポップアップと同じ内容を見られるようにするため)
    selected_date = request.args.get("date", "").strip()
    detail = common.build_calendar_day_detail(selected_date, store_names=store_names) if selected_date else None

    return render_template(
        "calendar.html",
        calendar=calendar_data,
        store_names=store_names,
        store_name=store_names[0] if len(store_names) == 1 else "",
        detail=detail,
    )


@calendar_bp.route("/day/<date_str>")
def day(date_str):
    """
    1日ぶんの詳細だけを返す(ポップアップがfetchで読み込む部分)。

    表示の組み立てはページ側と同じテンプレートを使い回す。
    JSON+JSで組み立て直すと、書式の直しが2か所に分かれてしまうため。
    """
    detail = common.build_calendar_day_detail(date_str, store_names=_selected_stores())
    if not detail:
        return "<p class='hint'>日付を読み取れませんでした。</p>", 400
    return render_template("_calendar_day.html", detail=detail, embedded=True)
