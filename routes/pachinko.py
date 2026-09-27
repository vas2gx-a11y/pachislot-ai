from datetime import datetime

from flask import Blueprint, abort, flash, make_response, redirect, render_template, request, url_for

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


def _photo_form_context(photo=None):
    """登録・編集フォームに渡すもの。機種のボーダーは、機種を選んだときに欄を埋めるのに使う。"""
    machines, _ = pachinko_info.load_all()
    args = request.args
    return {
        "photo": photo,
        "machines": [{key: m[key] for key in ("machine_id", "name", "border_equiv", "border_28", "border_33")}
                     for m in machines],
        "border_points": [{"key": key, "balls": balls} for key, balls in pachinko_info.BORDER_POINTS],
        "kinds": common.PHOTO_KINDS,
        "parts": common.PHOTO_PARTS,
        "store_names": [s["name"] for s in common.list_store_names()],
        "max_image_chars": common.PHOTO_CELL_CHARS * common.PHOTO_IMAGE_COLUMNS,
        "max_thumb_chars": common.PHOTO_THUMB_MAX_CHARS,
        # 回転率計算から「この回転率で写真を記録」で来たときの初期値
        "prefill": {
            "machine_id": args.get("machine_id", ""),
            "kind": args.get("kind", ""),
            "spins_per_k": args.get("rate", ""),
            "border": args.get("border", ""),
            "spins": args.get("spins", ""),
            "invest_yen": args.get("invest", ""),
            "date": datetime.now().strftime("%Y-%m-%d"),
        },
    }


def _machine_name_of(machine_id, fallback):
    # 機種名はJSON側で直ることがあるが、写真には撮ったときの名前を残す(機種データを消しても一覧で読めるように)
    machine = pachinko_info.load(machine_id) if machine_id else None
    return machine["name"] if machine else fallback


@pachinko_bp.route("/photos")
def photos():
    """
    釘写真帳。機種ごとに基準写真と実戦写真(回転率の高い順)を並べる。
    写真はシートに置くので、他の画面と同じくユーザーごとに分かれている(common の load_pachinko_photos)。
    """
    machine_id = request.args.get("machine_id", "")
    all_groups = common.build_photo_album()
    return render_template(
        "pachinko_photos.html",
        groups=[g for g in all_groups if not machine_id or g["machine_id"] == machine_id],
        machine_options=[{"machine_id": g["machine_id"], "name": g["machine_name"]}
                         for g in all_groups if g["machine_id"]],
        machine_id=machine_id,
    )


@pachinko_bp.route("/photos/new", methods=["GET", "POST"])
def photo_new():
    if request.method == "POST":
        form = request.form.to_dict()
        form["machine_name"] = _machine_name_of(form.get("machine_id"), form.get("machine_name", ""))
        photo_id, error = common.save_pachinko_photo(form, form.get("image_data"), form.get("thumb_data"))
        if error:
            flash(error)
            return redirect(url_for("pachinko.photo_new", machine_id=form.get("machine_id", "")))
        return redirect(url_for("pachinko.photo_detail", photo_id=photo_id))
    return render_template("pachinko_photo_form.html", **_photo_form_context())


@pachinko_bp.route("/photos/<photo_id>")
def photo_detail(photo_id):
    photo = common.find_pachinko_photo(photo_id)
    if not photo:
        abort(404)
    candidates = common.compare_candidates(photo)
    requested = request.args.get("with", "")
    partner = next((p for p in candidates if p["photo_id"] == requested), candidates[0] if candidates else None)
    return render_template("pachinko_photo_detail.html", photo=photo, candidates=candidates, partner=partner)


@pachinko_bp.route("/photos/<photo_id>/edit", methods=["GET", "POST"])
def photo_edit(photo_id):
    """回転率やメモの直し。画像は差し替えない(別の写真なら新しく登録するほうが比較の履歴が崩れない)。"""
    photo = common.find_pachinko_photo(photo_id)
    if not photo:
        abort(404)
    if request.method == "POST":
        form = request.form.to_dict()
        form["photo_id"] = photo_id
        form["machine_name"] = _machine_name_of(form.get("machine_id"), photo["machine_name"])
        _, error = common.save_pachinko_photo(form)
        if error:
            flash(error)
            return redirect(url_for("pachinko.photo_edit", photo_id=photo_id))
        return redirect(url_for("pachinko.photo_detail", photo_id=photo_id))
    return render_template("pachinko_photo_form.html", **_photo_form_context(photo))


@pachinko_bp.route("/photos/<photo_id>/image.jpg")
def photo_image(photo_id):
    data = common.load_pachinko_photo_image(photo_id)
    if data is None:
        abort(404)
    response = make_response(data)
    response.mimetype = "image/jpeg"
    # 画像は登録後に差し替えないので、同じURLはずっと同じ中身。ブラウザに持たせてシートへの読み込みを減らす
    # (本人にしか見せない画像なので、共有のキャッシュには置かせない)
    response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
    return response


@pachinko_bp.route("/photos/<photo_id>/delete", methods=["POST"])
def photo_delete(photo_id):
    photo = common.find_pachinko_photo(photo_id)
    if not photo:
        abort(404)
    if not common.delete_pachinko_photo(photo_id):
        flash("写真を削除できませんでした。時間をおいてもう一度お試しください。")
        return redirect(url_for("pachinko.photo_detail", photo_id=photo_id))
    return redirect(url_for("pachinko.photos", machine_id=photo["machine_id"]))


@pachinko_bp.route("/<machine_id>")
def detail(machine_id):
    machine = pachinko_info.load(machine_id)
    if not machine:
        abort(404)
    photo_count = sum(1 for p in common.load_pachinko_photos() if p["machine_id"] == machine_id)
    return render_template("pachinko_detail.html", machine=machine, photo_count=photo_count)
