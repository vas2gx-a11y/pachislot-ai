"""
機種情報(解析まとめ)ページ。machine_data/<id>.json を読んでそのまま描く。

ページの中身はJSONが正で、この画面から編集はしない。
新台は machine_data/ にJSONを1つ置けば一覧と詳細ページに出る(書き方は machine_data/README.md)。
判別スペックへの取り込みだけはボタンで明示的に行う(理由は machine_info.py の冒頭)。
"""

from datetime import date

from flask import Blueprint, abort, flash, redirect, render_template, url_for

import judge_db
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
    judge_row = judge_db.query_db(
        "SELECT id, updated_at FROM judge_machines WHERE name = ?", (m.get("name"),), one=True
    )

    return render_template(
        "machine_info.html",
        m=m,
        problems=machine_info.validate(m, machine_id),
        source_no=source_no,
        payout=_payout_range(m),
        judge_row=judge_row,
        **HELPERS,
    )


@machine_info_bp.route("/<machine_id>/import_judge", methods=("POST",))
def import_judge(machine_id):
    """機種JSONの判別データを判別スペックに取り込む。同名の機種があれば上書きする。"""
    m = machine_info.load(machine_id)
    if m is None:
        abort(404)

    problems = machine_info.validate(m, machine_id)
    if problems:
        flash("JSONに不備があるため取り込めません: " + " / ".join(problems))
        return redirect(url_for("machine_info.detail", machine_id=machine_id))

    try:
        spec, skipped = machine_info.to_judge_spec(m)
    except ValueError as e:
        flash(f"取り込めません: {e}")
        return redirect(url_for("machine_info.detail", machine_id=machine_id))

    _, created = judge_db.upsert_machine(spec)

    counts = (f"判別要素{len(spec['judge_items'])}件・選択肢型{len(spec['categorical_groups'])}件・"
              f"確定演出{len(spec['confirmations'])}件")
    flash(f"「{spec['name']}」の判別スペックを{'登録' if created else '上書き'}しました（{counts}）。")
    if skipped:
        flash("値が揃わず取り込まなかった項目: " + " / ".join(skipped))
    return redirect(url_for("judge.index", machine_name=spec["name"]))
