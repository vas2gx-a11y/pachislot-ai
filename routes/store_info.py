"""
店舗情報ページ。store_data/<id>.json(基本情報)とシート(営業データ)を1ページに並べる。

基本情報はJSONが正で、この画面から編集はしない(集め方は tools/store_collect.py)。
営業データはシートが正。直近の一覧に加えて、日別の推移・イベ日の信頼度・台別の傾向もここで見る
(以前は別ページの「店舗傾向」にあったが、店の情報と傾向を行き来する手間をなくすため統合した)。
URLを /stores にしないのは、店舗名の変更・統合を行う「店舗の管理」が既に使っているため。
"""

from flask import Blueprint, abort, render_template, request

import common
import store_info
from routes.store_trends import PERIOD_CHOICES, DEFAULT_DAYS, _parse_days

store_info_bp = Blueprint("store_info", __name__, url_prefix="/store_info")

# 一覧として並べる日数。それより前はグラフと「日ごとの取り込み内容」で見る
RECENT_DAYS = 14


def _operation_summary(sheet_store_name):
    """シート側の営業データの概要。どれか読めなくてもページ自体は出す。"""
    daily = common.load_store_daily(sheet_store_name)
    units = common.load_store_units(sheet_store_name)
    events = common.load_store_events().get(sheet_store_name)

    unit_dates = sorted({r["date"] for r in units}, reverse=True)
    latest_units = [r for r in units if unit_dates and r["date"] == unit_dates[0]]
    return {
        "daily": daily[:RECENT_DAYS],
        "daily_count": len(daily),
        "unit_date_count": len(unit_dates),
        "unit_row_count": len(units),
        "latest_unit_date": unit_dates[0] if unit_dates else None,
        "latest_units": sorted(latest_units, key=lambda r: r["difference_slabs"] or 0, reverse=True),
        "events": events,
    }


def _trend_summary(sheet_store_name, days, with_machine_details):
    """
    店の全台データの傾向。
    日別は対象期間が長いほど曜日・イベ日の傾向が安定するので、画面の期間指定とは別に直近1年と全期間を出す。
    台別は台ごとの傾向を見るためのものなので、画面の期間指定に合わせる。
    """
    return {
        "daily": common.build_store_daily_trends(sheet_store_name, days=365),
        # 直近1年だけだと「最近たまたま強い/弱い」を「そういうイベントだ」と誤読しやすいため並べて比べる
        "daily_all": common.build_store_daily_trends(sheet_store_name, days=0),
        "units": common.build_store_unit_trends(sheet_store_name, days=days,
                                                include_machine_details=with_machine_details),
    }


def _sort_by(rows, key):
    """新しい順・多い順に並べる。値が不明(None)の行は、JinjaのsortだとNoneと比較できず落ちるので末尾に回す。"""
    return sorted(rows or [], key=lambda r: (r.get(key) is not None, r.get(key) or 0), reverse=True)


@store_info_bp.route("/")
def index():
    stores, errors = store_info.load_all()
    return render_template("store_info_list.html", stores=stores, errors=errors,
                           field_labels=store_info.FIELD_LABELS)


@store_info_bp.route("/<store_id>")
def detail(store_id):
    s = store_info.load(store_id)
    if s is None:
        abort(404)

    # 出典は本文中で [1][2] と番号で参照するので、登録順に番号を振っておく
    source_no = {src["id"]: i for i, src in enumerate(s.get("sources", []), start=1)}
    sheet_store_name = s.get("sheet_store_name") or s["name"]
    days = _parse_days(request.args.get("days", DEFAULT_DAYS))
    known = sum(1 for k in store_info.FIELD_KEYS if (s["info"].get(k) or {}).get("status") != "不明")

    return render_template(
        "store_info.html",
        s=s,
        info=s.get("info") or {},
        problems=store_info.validate(s, store_id),
        source_no=source_no,
        known=known,
        field_count=len(store_info.FIELD_KEYS),
        field_labels=store_info.FIELD_LABELS,
        ops=_operation_summary(sheet_store_name),
        tr=_trend_summary(sheet_store_name, days, with_machine_details=bool(request.args.get("details"))),
        days=days,
        period_choices=PERIOD_CHOICES,
        history=list(reversed(s.get("history", [])))[:50],
        sort_by=_sort_by,
    )
