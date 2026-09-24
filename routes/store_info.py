"""
店舗情報ページ。store_data/<id>.json(基本情報)とシート(営業データ)を1ページに並べる。

基本情報はJSONが正で、この画面から編集はしない(集め方は tools/store_collect.py)。
営業データはシートが正で、ここでは直近の概要だけを見せ、詳しくは店舗傾向ページへ渡す。
URLを /stores にしないのは、店舗名の変更・統合を行う「店舗の管理」が既に使っているため。
"""

from flask import Blueprint, abort, render_template

import common
import store_info

store_info_bp = Blueprint("store_info", __name__, url_prefix="/store_info")

# 概要として並べる日数。これより前は店舗傾向ページで見る
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
        ops=_operation_summary(s.get("sheet_store_name") or s["name"]),
        history=list(reversed(s.get("history", [])))[:50],
        sort_by=_sort_by,
    )
