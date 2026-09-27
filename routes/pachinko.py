from flask import Blueprint, abort, render_template, request

import pachinko_info

pachinko_bp = Blueprint("pachinko", __name__, url_prefix="/pachinko")


@pachinko_bp.route("/")
def index():
    """機種データの一覧。スペックタイプの絞り込みと検索は画面側(JS)でその場で行う。"""
    machines, errors = pachinko_info.load_all()
    return render_template(
        "pachinko_list.html",
        machines=machines,
        errors=errors,
        spec_types=pachinko_info.SPEC_TYPES,
    )


@pachinko_bp.route("/calc")
def calc():
    """
    ホールで打ちながら使う回転率の計算機。

    投資や回転数を入れるたびに結果が動くのが使い方なので、計算はブラウザ側だけで行う
    (判別画面と同じ理由)。機種のボーダーはページに埋め込んで渡す。
    """
    machines, _ = pachinko_info.load_all()
    return render_template(
        "pachinko_calc.html",
        machines=[
            {key: m[key] for key in ("machine_id", "name", "spec_type", "hit_prob",
                                     "border_equiv", "border_28", "border_33")}
            for m in machines
        ],
        border_points=[{"key": key, "balls": balls} for key, balls in pachinko_info.BORDER_POINTS],
        preselect_id=request.args.get("machine_id", ""),
    )


@pachinko_bp.route("/<machine_id>")
def detail(machine_id):
    machine = pachinko_info.load(machine_id)
    if not machine:
        abort(404)
    return render_template("pachinko_detail.html", machine=machine)
