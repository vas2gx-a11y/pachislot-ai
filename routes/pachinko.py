from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

import auth
import common

pachinko_bp = Blueprint("pachinko", __name__, url_prefix="/pachinko")

# 画面のフォームから受け取る文字列の列(数値の列は common.PACHINKO_NUMERIC_FIELDS)
_TEXT_FIELDS = ["name", "maker", "spec_type", "yutime_note", "morning_lamp_note",
                "technique_note", "note", "source"]


def _blank_machine():
    """新規登録フォーム用の空の機種(テンプレートは未定義の属性をエラーにするので、全項目を持たせる)"""
    machine = {h: "" for h in common.PACHINKO_HEADERS}
    machine.update({field: None for field in common.PACHINKO_NUMERIC_FIELDS})
    machine["effects"] = []
    return machine


def _machine_from_form(form):
    machine = _blank_machine()
    machine["machine_id"] = form.get("machine_id", "").strip()
    for field in _TEXT_FIELDS:
        machine[field] = form.get(field, "").strip()
    for field in common.PACHINKO_NUMERIC_FIELDS:
        machine[field] = common._to_float_or_none(form.get(field))

    # 演出の期待度は行の数が決まっていないので、同じ名前の入力を並びで受け取って組み立てる。
    # 名前が空の行は「追加したが使わなかった行」なので捨てる
    effects = []
    for name, rate, memo in zip(form.getlist("effect_name"), form.getlist("effect_rate"),
                                form.getlist("effect_note")):
        if name.strip():
            effects.append({"name": name.strip(), "rate": common._to_float_or_none(rate),
                            "note": memo.strip()})
    machine["effects"] = effects
    return machine


@pachinko_bp.route("/")
def index():
    """機種データの一覧。スペックタイプの絞り込みと検索は画面側(JS)でその場で行う。"""
    return render_template(
        "pachinko_list.html",
        machines=common.load_pachinko_machines(),
        spec_types=common.PACHINKO_SPEC_TYPES,
    )


@pachinko_bp.route("/<machine_id>")
def detail(machine_id):
    machine = common.find_pachinko_machine(machine_id)
    if not machine:
        abort(404)
    return render_template("pachinko_detail.html", machine=machine)


@pachinko_bp.route("/new")
@auth.admin_required
def new():
    return render_template("pachinko_edit.html", machine=_blank_machine(), spec_types=common.PACHINKO_SPEC_TYPES)


@pachinko_bp.route("/<machine_id>/edit")
@auth.admin_required
def edit(machine_id):
    machine = common.find_pachinko_machine(machine_id)
    if not machine:
        abort(404)
    return render_template("pachinko_edit.html", machine=machine, spec_types=common.PACHINKO_SPEC_TYPES)


@pachinko_bp.route("/save", methods=["POST"])
@auth.admin_required
def save():
    machine = _machine_from_form(request.form)
    ok, message, machine_id = common.save_pachinko_machine(machine)
    flash(message)
    if not ok:
        # 入力し直しにならないよう、送られてきた内容のままフォームに戻す
        return render_template("pachinko_edit.html", machine=machine,
                               spec_types=common.PACHINKO_SPEC_TYPES)
    return redirect(url_for("pachinko.detail", machine_id=machine_id))


@pachinko_bp.route("/<machine_id>/delete", methods=["POST"])
@auth.admin_required
def delete(machine_id):
    ok, message = common.delete_pachinko_machine(machine_id)
    flash(message)
    if not ok:
        return redirect(url_for("pachinko.detail", machine_id=machine_id))
    return redirect(url_for("pachinko.index"))


@pachinko_bp.route("/calc")
def calc():
    """
    ホールで打ちながら使う回転率の計算機。

    投資や回転数を入れるたびに結果が動くのが使い方なので、計算はブラウザ側だけで行う
    (判別画面と同じ理由)。機種のボーダーはページに埋め込んで渡す。
    """
    machines = [
        {key: m[key] for key in ("machine_id", "name", "spec_type", "hit_prob",
                                 "border_equiv", "border_28", "border_33")}
        for m in common.load_pachinko_machines()
    ]
    return render_template(
        "pachinko_calc.html",
        machines=machines,
        border_points=[{"key": key, "balls": balls} for key, balls in common.PACHINKO_BORDER_POINTS],
        preselect_id=request.args.get("machine_id", ""),
    )
