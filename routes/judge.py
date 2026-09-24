"""
設定判別(ベイズ推定)の画面。

推定そのものはブラウザ側(static/js/bayes.js)で完結する。
入力するたびに事後確率が動くのが判別の使い方なので、
1回ごとにサーバーへ往復させると打ちながら使えなくなるため。
サーバー側の役割は「機種スペックを配ること」だけで、スペックは機種情報JSON
(machine_data/*.json)から都度組み立てる(理由は machine_info.py の冒頭)。
"""

from flask import Blueprint, jsonify, render_template, request

import machine_info

judge_bp = Blueprint("judge", __name__, url_prefix="/judge")


@judge_bp.route("/")
def index():
    """
    機種スペックはページ生成時に埋め込んで渡す。

    別途APIを叩かせて取り込ませる形にもできるが、そうすると
    「サーバーとブラウザのどちらのスペックが正か」が曖昧になる。
    スペックは常にサーバーが正、ブラウザのlocalStorageに置くのは
    店舗条件と判別ログ(その端末の持ち物)だけ、と役割を分けている。
    """
    machines = machine_info.load_for_judge()

    # 記録一覧など他ページから ?machine_name= 付きで飛んできたときに機種を選んだ状態で開く。
    # 手入力の機種名なので、通称(aliases)や部分一致でも拾う。
    requested = (request.args.get("machine_name") or "").strip()
    found = machine_info.find_by_name(requested)
    judge_ids = {m["id"] for m in machines}
    preselect_id = found["id"] if found and found["id"] in judge_ids else None

    return render_template(
        "judge.html",
        machines=machines,
        preselect_id=preselect_id,
        requested_machine_name=requested,
    )


@judge_bp.route("/api/machines")
def api_machines():
    """判別スペックのJSON。画面には埋め込み済みだが、他クライアントからも読めるようにしておく。"""
    machines = machine_info.load_for_judge()
    return jsonify({"count": len(machines), "machines": machines})
