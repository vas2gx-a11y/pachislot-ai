from flask import Blueprint, abort, jsonify, render_template, request

import common
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
    return render_template(
        "pachinko_calc.html",
        machines=_calc_machines(),
        border_points=_border_points(),
        preselect_id=request.args.get("machine_id", ""),
    )


def _calc_machines():
    """回転率計算と振り返りの画面に埋め込む機種データ(ボーダーの換算に要る項目だけ)。"""
    machines, _ = pachinko_info.load_all()
    return [
        {key: m[key] for key in ("machine_id", "name", "spec_type", "hit_prob",
                                 "border_equiv", "border_28", "border_33")}
        for m in machines
    ]


def _border_points():
    return [{"key": key, "balls": balls} for key, balls in pachinko_info.BORDER_POINTS]


@pachinko_bp.route("/review")
def review():
    """
    AI分析のパチンコ期待値・振り返り。

    期待値は回転率計算の入力(この端末の localStorage)からブラウザ側で出す。打ちながら開いて
    「このまま続けたらいくらの価値があるか」を見る使い方なので、回転率計算と同じく往復させない。
    保存を押したときだけ稼働をシートに残し、過去の稼働と並べてAIに改善点を出させる。
    """
    sessions = common.load_pachinko_sessions()
    names = [s["name"] for s in common.list_store_names()]
    store_names = names + [n for n in common.store_info_by_sheet_name() if n not in set(names)]
    return render_template(
        "pachinko_review.html",
        machines=_calc_machines(),
        border_points=_border_points(),
        store_names=store_names,
        sessions=sessions[:30],
        store_summary=common.build_pachinko_store_summary(sessions),
    )


@pachinko_bp.route("/review/api", methods=("POST",))
def review_api():
    """稼働を計算し直してAIに振り返らせ、振り返りごと保存する。AIが失敗しても稼働は保存する。"""
    session, error = common.build_pachinko_session(request.get_json(silent=True) or {})
    if error:
        return jsonify({"error": error})
    feedback, ai_error = common.pachinko_session_feedback(session, common.load_pachinko_sessions())
    session["ai_feedback"] = feedback or ""
    if not common.save_pachinko_session(session):
        return jsonify({"error": "スプレッドシートへの保存に失敗しました。時間をおいてもう一度押してください。",
                        "feedback": feedback or ""})
    return jsonify({
        "feedback": feedback or "",
        "ai_error": ai_error or "",
        "session": {k: session[k] for k in ("rate", "border", "ev_per_spin", "work_yen",
                                            "below_border_ratio", "below_border_basis")},
    })


@pachinko_bp.route("/<machine_id>")
def detail(machine_id):
    machine = pachinko_info.load(machine_id)
    if not machine:
        abort(404)
    return render_template("pachinko_detail.html", machine=machine)
