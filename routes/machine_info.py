"""
機種情報(解析まとめ)ページ。machine_data/<id>.json を読んでそのまま描く。

ページの中身はJSONが正で、この画面から編集はしない(理由は machine_info.py の冒頭)。
新台は machine_data/ にJSONを1つ置けば、一覧・詳細ページ・設定判別・設定推測のすべてに出る
(書き方は machine_data/README.md)。
"""

from datetime import date

from flask import Blueprint, abort, render_template

import machine_info

machine_info_bp = Blueprint("machine_info", __name__, url_prefix="/info")


# ---------------------------------------------------------------------------
# 表示用の変換
# ---------------------------------------------------------------------------
# 不明な値を推測で埋めないことがこのデータの前提なので、
# null や {"status": ...} は必ず「未公表」「情報確認中」などの文言として見せる。

def _is_unknown(v):
    return v is None or (isinstance(v, dict) and "status" in v)


def _unknown_label(v):
    return v["status"] if isinstance(v, dict) and v.get("status") else "未公表"


def _fmt_num(v, fmt):
    if fmt == "fraction":
        # 整数で公表されている値(1/269など)に小数を足すと、ない精度があるように見えるのでそのまま出す
        return f"1/{v}" if isinstance(v, int) else f"1/{v:.1f}"
    if fmt == "percent":
        return f"{v:.1f}%"
    return str(v)


def _fmt_date(v):
    try:
        d = date.fromisoformat(v)
    except (TypeError, ValueError):
        return None
    return f"{d.year}年{d.month}月{d.day}日"


def _payout_range(m):
    row = next((r for r in (m.get("spec") or {}).get("rows", []) if r.get("key") == "payout"), None)
    values = [v for v in (row or {}).get("values", {}).values() if isinstance(v, (int, float))]
    return (min(values), max(values)) if values else None


def _split_by_setting(rows):
    """設定差の無い行(value)と設定別の行(values)に分ける。
    Jinjaの selectattr('values') は dict.values メソッドを拾ってしまい判定できないため、ここで分ける。
    """
    common = [r for r in rows if "values" not in r]
    per_setting = [r for r in rows if "values" in r]
    return common, per_setting


HELPERS = {
    "split_by_setting": _split_by_setting,
    "is_unknown": _is_unknown,
    "unknown_label": _unknown_label,
    "fmt_num": _fmt_num,
    "fmt_date": _fmt_date,
}


# ---------------------------------------------------------------------------
# 画面
# ---------------------------------------------------------------------------

@machine_info_bp.route("/")
def index():
    machines, errors = machine_info.load_all()
    return render_template(
        "machine_info_list.html",
        machines=machines, errors=errors, payout_range=_payout_range, **HELPERS,
    )


@machine_info_bp.route("/<machine_id>")
def detail(machine_id):
    m = machine_info.load(machine_id)
    if m is None:
        abort(404)

    # 出典は本文中で [1][2] と番号で参照するので、登録順に番号を振っておく
    source_no = {s["id"]: i for i, s in enumerate(m.get("sources", []), start=1)}
    problems = machine_info.validate(m, machine_id)

    # 判別ページと同じ変換を通し、判別に使えない項目があればページ上で分かるようにする
    judge_spec, judge_skipped = None, []
    if not problems:
        try:
            judge_spec, judge_skipped = machine_info.to_client_spec(m)
        except ValueError as e:
            judge_skipped = [str(e)]

    return render_template(
        "machine_info.html",
        m=m,
        problems=problems,
        source_no=source_no,
        payout=_payout_range(m),
        judge_spec=judge_spec,
        judge_skipped=judge_skipped,
        **HELPERS,
    )

