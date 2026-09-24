"""
設定判別(ベイズ推定)の画面と、判別スペックの管理。

推定そのものはブラウザ側(static/js/bayes.js)で完結する。
入力するたびに事後確率が動くのが判別の使い方なので、
1回ごとにサーバーへ往復させると打ちながら使えなくなるため。
サーバー側の役割は「機種スペックを配ること」と「その登録画面」に絞っている。
"""

from flask import Blueprint, flash, jsonify, redirect, render_template, request, url_for

import judge_db

judge_bp = Blueprint("judge", __name__, url_prefix="/judge")


# ---------------------------------------------------------------------------
# 判別ページ
# ---------------------------------------------------------------------------

@judge_bp.route("/")
def index():
    """
    機種スペックはページ生成時に埋め込んで渡す。

    別途APIを叩かせて取り込ませる形にもできるが、そうすると
    「サーバーとブラウザのどちらのスペックが正か」が曖昧になる。
    スペックは常にサーバーが正、ブラウザのlocalStorageに置くのは
    店舗条件と判別ログ(その端末の持ち物)だけ、と役割を分けている。
    """
    machines = judge_db.load_machines_for_client()

    # 記録一覧など他ページから ?machine_name= 付きで飛んできたときに機種を選んだ状態で開く。
    # 完全一致で見つからなければ、登録名を含む/含まれる関係で拾う(表記ゆれ対策)。
    requested = (request.args.get("machine_name") or "").strip()
    preselect_id = _match_machine_id(machines, requested)

    return render_template(
        "judge.html",
        machines=machines,
        preselect_id=preselect_id,
        requested_machine_name=requested,
    )


def _match_machine_id(machines, name):
    """機種名から登録済み機種を探す。見つからなければ None。"""
    if not name:
        return None

    for m in machines:
        if m["name"] == name:
            return m["id"]

    # 部分一致は複数当たりうるので、より具体的(名前が長い)ものを採用する
    candidates = [m for m in machines if m["name"] and (m["name"] in name or name in m["name"])]
    if not candidates:
        return None
    return max(candidates, key=lambda m: len(m["name"]))["id"]


@judge_bp.route("/api/machines")
def api_machines():
    """判別スペックのJSON。画面には埋め込み済みだが、他クライアントからも読めるようにしておく。"""
    machines = judge_db.load_machines_for_client()
    return jsonify({"count": len(machines), "machines": machines})


# ---------------------------------------------------------------------------
# 判別スペックの管理
# ---------------------------------------------------------------------------

@judge_bp.route("/machines")
def machines():
    rows = judge_db.query_db(
        """SELECT m.*,
                  (SELECT COUNT(*) FROM judge_items j WHERE j.machine_id = m.id) AS judge_count,
                  (SELECT COUNT(*) FROM categorical_groups g WHERE g.machine_id = m.id) AS group_count,
                  (SELECT COUNT(*) FROM confirmation_items c WHERE c.machine_id = m.id) AS confirm_count
           FROM judge_machines m
           ORDER BY m.name"""
    )
    return render_template("judge_machines.html", machines=rows)


@judge_bp.route("/machines/new", methods=("GET", "POST"))
def machine_new():
    if request.method == "POST":
        data, error = _parse_machine_form(request.form)
        if error:
            flash(error)
            return _render_form(request.form, is_new=True)

        machine_id = judge_db.execute_db(
            """INSERT INTO judge_machines
               (name, maker, payout1, payout2, payout3, payout4, payout5, payout6)
               VALUES (?,?,?,?,?,?,?,?)""",
            (data["name"], data["maker"], *data["payouts"]),
        )
        _save_children(machine_id, data)
        flash(f"「{data['name']}」の判別スペックを登録しました。")
        return redirect(url_for("judge.machines"))

    return render_template(
        "judge_machine_form.html", machine=None, judge_items=[],
        categorical_groups=[], confirmations=[], is_new=True,
    )


@judge_bp.route("/machines/<int:machine_id>/edit", methods=("GET", "POST"))
def machine_edit(machine_id):
    machine = judge_db.query_db(
        "SELECT * FROM judge_machines WHERE id = ?", (machine_id,), one=True
    )
    if machine is None:
        flash("指定された機種が見つかりません。")
        return redirect(url_for("judge.machines"))

    if request.method == "POST":
        data, error = _parse_machine_form(request.form)
        if error:
            flash(error)
            return _render_form(request.form, is_new=False, machine_id=machine_id)

        judge_db.execute_db(
            """UPDATE judge_machines SET
                 name=?, maker=?,
                 payout1=?, payout2=?, payout3=?, payout4=?, payout5=?, payout6=?,
                 updated_at=datetime('now','localtime')
               WHERE id=?""",
            (data["name"], data["maker"], *data["payouts"], machine_id),
        )
        _save_children(machine_id, data)
        flash(f"「{data['name']}」の判別スペックを更新しました。")
        return redirect(url_for("judge.machines"))

    return render_template(
        "judge_machine_form.html",
        machine=machine,
        judge_items=judge_db.query_db(
            "SELECT * FROM judge_items WHERE machine_id = ? ORDER BY sort_order, id", (machine_id,)
        ),
        categorical_groups=_categorical_groups_of(machine_id),
        confirmations=judge_db.query_db(
            "SELECT * FROM confirmation_items WHERE machine_id = ? ORDER BY sort_order, id",
            (machine_id,),
        ),
        is_new=False,
    )


@judge_bp.route("/machines/<int:machine_id>/delete", methods=("POST",))
def machine_delete(machine_id):
    machine = judge_db.query_db(
        "SELECT name FROM judge_machines WHERE id = ?", (machine_id,), one=True
    )
    if machine is None:
        flash("指定された機種が見つかりません。")
        return redirect(url_for("judge.machines"))

    # 子テーブルは ON DELETE CASCADE で消えるが、選択肢だけはグループ経由の
    # 2段カスケードになるため、外部キーが無効な環境でも取り残さないよう明示的に消す
    for grp in judge_db.query_db(
        "SELECT id FROM categorical_groups WHERE machine_id = ?", (machine_id,)
    ):
        judge_db.execute_db("DELETE FROM categorical_options WHERE group_id = ?", (grp["id"],))
    judge_db.execute_db("DELETE FROM categorical_groups WHERE machine_id = ?", (machine_id,))
    judge_db.execute_db("DELETE FROM confirmation_items WHERE machine_id = ?", (machine_id,))
    judge_db.execute_db("DELETE FROM judge_items WHERE machine_id = ?", (machine_id,))
    judge_db.execute_db("DELETE FROM judge_machines WHERE id = ?", (machine_id,))

    flash(f"「{machine['name']}」を削除しました。")
    return redirect(url_for("judge.machines"))


def _render_form(form, is_new, machine_id=None):
    """入力エラーで差し戻すときは、打ち直しにならないよう入力値のまま描き直す。"""
    return render_template(
        "judge_machine_form.html",
        machine=form,
        machine_id=machine_id,
        judge_items=_judge_items_from_form(form),
        categorical_groups=_categorical_from_form(form),
        confirmations=_confirmations_from_form(form),
        is_new=is_new,
    )


# ---------------------------------------------------------------------------
# フォームの解釈と保存
# ---------------------------------------------------------------------------

def _to_float(value):
    value = (value or "").strip()
    if value == "":
        return None
    try:
        return float(value)
    except ValueError:
        raise ValueError(value)


def _parse_machine_form(form):
    """フォームの入力を検証して、DBに渡せる形に整える。"""
    name = (form.get("name") or "").strip()
    if not name:
        return None, "機種名は必須です。"

    try:
        payouts = [_to_float(form.get(f"payout{i}")) for i in range(1, 7)]
    except ValueError as e:
        return None, f"機械割の入力が数値ではありません: {e}"

    for i, value in enumerate(payouts, start=1):
        if value is not None and value <= 0:
            return None, f"設定{i}の機械割は0より大きい値を入力してください。"

    judge_items, error = _parse_judge_items(form)
    if error:
        return None, error

    categorical_groups, error = _parse_categorical_groups(form)
    if error:
        return None, error

    confirmations, error = _parse_confirmations(form)
    if error:
        return None, error

    return {
        "name": name,
        "maker": (form.get("maker") or "").strip() or None,
        "payouts": payouts,
        "judge_items": judge_items,
        "categorical_groups": categorical_groups,
        "confirmations": confirmations,
    }, None


def _parse_judge_items(form):
    """判別要素の入力を行ごとにまとめる。
    フォームは judge_name[] / judge_type[] / judge_base[] / judge_denom1〜6[] を同数送る前提。
    """
    names = form.getlist("judge_name[]")
    types = form.getlist("judge_type[]")
    bases = form.getlist("judge_base[]")
    denominator_lists = [form.getlist(f"judge_denom{i}[]") for i in range(1, 7)]

    items = []
    for idx, raw_name in enumerate(names):
        item_name = (raw_name or "").strip()
        raw_values = [col[idx] if idx < len(col) else "" for col in denominator_lists]

        # 名前も数値も未入力の行は、追加ボタンで増やしただけの空行として無視する
        if not item_name and not any((v or "").strip() for v in raw_values):
            continue

        if not item_name:
            return None, "判別要素の名前を入力してください。"

        try:
            denominators = [_to_float(v) for v in raw_values]
        except ValueError as e:
            return None, f"判別要素「{item_name}」の数値が不正です: {e}"

        if any(d is None for d in denominators):
            return None, f"判別要素「{item_name}」は設定1〜6すべての確率を入力してください。"
        if any(d <= 1 for d in denominators):
            return None, f"判別要素「{item_name}」の確率(分母)は1より大きい値を入力してください。"

        item_type = types[idx] if idx < len(types) else "koyaku"
        if item_type not in ("koyaku", "bonus", "ratio"):
            item_type = "koyaku"

        denom_base = bases[idx] if idx < len(bases) else "total"
        if denom_base not in ("total", "normal", "custom"):
            denom_base = "total"
        # 成功率型は総回転数と母数が違うので、必ず試行回数を個別入力させる
        if item_type == "ratio":
            denom_base = "custom"

        items.append({
            "name": item_name,
            "item_type": item_type,
            "denom_base": denom_base,
            "denominators": denominators,
        })

    return items, None


def _parse_categorical_groups(form):
    """選択肢型の入力をグループ単位にまとめる。
    グループと選択肢の対応は cat_group_idx[] で保持する。
    """
    group_names = form.getlist("cat_group_name[]")
    opt_group_idx = form.getlist("cat_group_idx[]")
    opt_names = form.getlist("cat_opt_name[]")
    prob_lists = [form.getlist(f"cat_p{i}[]") for i in range(1, 7)]

    groups = [{"name": (n or "").strip(), "options": []} for n in group_names]

    for oi, raw_opt in enumerate(opt_names):
        opt_name = (raw_opt or "").strip()
        raw_values = [col[oi] if oi < len(col) else "" for col in prob_lists]

        if not opt_name and not any((v or "").strip() for v in raw_values):
            continue

        try:
            gi = int(opt_group_idx[oi]) if oi < len(opt_group_idx) else -1
        except ValueError:
            continue
        if gi < 0 or gi >= len(groups):
            continue

        if not opt_name:
            return None, "終了画面などの選択肢名を入力してください。"

        try:
            probabilities = [_to_float(v) for v in raw_values]
        except ValueError as e:
            return None, f"選択肢「{opt_name}」の出現率が不正です: {e}"

        if any(p is None for p in probabilities):
            return None, f"選択肢「{opt_name}」は設定1〜6すべての出現率を入力してください。"
        if any(p < 0 for p in probabilities):
            return None, f"選択肢「{opt_name}」の出現率は0以上で入力してください。"

        groups[gi]["options"].append({"name": opt_name, "probabilities": probabilities})

    result = []
    for g in groups:
        if not g["options"]:
            continue  # 追加ボタンで増やしただけの空グループ
        if not g["name"]:
            return None, "判別グループの名前を入力してください。"
        if len(g["options"]) < 2:
            return None, f"「{g['name']}」は選択肢が1つしかないため判別に使えません。2つ以上登録するか削除してください。"

        for s_idx in range(6):
            if sum(o["probabilities"][s_idx] for o in g["options"]) <= 0:
                return None, f"「{g['name']}」の設定{s_idx + 1}は全ての出現率が0です。"

        result.append(g)

    return result, None


def _parse_confirmations(form):
    """機種固有の確定・否定演出をまとめる。"""
    groups = form.getlist("conf_group[]")
    names = form.getlist("conf_name[]")
    masks = [form.getlist(f"conf_s{i}[]") for i in range(1, 7)]

    items = []
    for idx, raw_name in enumerate(names):
        name = (raw_name or "").strip()
        group = (groups[idx] if idx < len(groups) else "").strip()

        if not name and not group:
            continue
        if not name:
            return None, "確定演出の名前を入力してください。"
        if not group:
            return None, f"確定演出「{name}」のグループ名を入力してください。"

        # チェックボックスは選択された行の添字だけが送られてくるので、値で判定する
        flags = [1 if str(idx) in masks[i] else 0 for i in range(6)]

        if all(f == 0 for f in flags):
            return None, f"確定演出「{name}」は全ての設定を否定しています。少なくとも1つは残してください。"
        if all(f == 1 for f in flags):
            return None, f"確定演出「{name}」は何も否定していません。該当する設定だけを選んでください。"

        items.append({"group": group, "name": name, "flags": flags})

    return items, None


def _save_children(machine_id, data):
    # 機種情報JSONからの取り込みと同じ処理を通すため、実体は judge_db 側にある
    judge_db.save_children(machine_id, data)


def _categorical_groups_of(machine_id):
    groups = judge_db.query_db(
        "SELECT * FROM categorical_groups WHERE machine_id = ? ORDER BY sort_order, id",
        (machine_id,),
    )
    return [
        {
            "name": g["name"],
            "options": judge_db.query_db(
                "SELECT * FROM categorical_options WHERE group_id = ? ORDER BY sort_order, id",
                (g["id"],),
            ),
        }
        for g in groups
    ]


# --- 入力エラーで差し戻すときの復元 --------------------------------------------

def _judge_items_from_form(form):
    names = form.getlist("judge_name[]")
    types = form.getlist("judge_type[]")
    bases = form.getlist("judge_base[]")
    denominator_lists = [form.getlist(f"judge_denom{i}[]") for i in range(1, 7)]

    restored = []
    for idx, name in enumerate(names):
        row = {
            "name": name,
            "item_type": types[idx] if idx < len(types) else "koyaku",
            "denom_base": bases[idx] if idx < len(bases) else "total",
        }
        for i, column in enumerate(denominator_lists, start=1):
            row[f"denom{i}"] = column[idx] if idx < len(column) else ""
        restored.append(row)
    return restored


def _categorical_from_form(form):
    group_names = form.getlist("cat_group_name[]")
    opt_group_idx = form.getlist("cat_group_idx[]")
    opt_names = form.getlist("cat_opt_name[]")
    prob_lists = [form.getlist(f"cat_p{i}[]") for i in range(1, 7)]

    groups = [{"name": n, "options": []} for n in group_names]
    for oi, name in enumerate(opt_names):
        try:
            gi = int(opt_group_idx[oi]) if oi < len(opt_group_idx) else -1
        except ValueError:
            continue
        if gi < 0 or gi >= len(groups):
            continue
        row = {"name": name}
        for i, col in enumerate(prob_lists, start=1):
            row[f"p{i}"] = col[oi] if oi < len(col) else ""
        groups[gi]["options"].append(row)
    return groups


def _confirmations_from_form(form):
    groups = form.getlist("conf_group[]")
    names = form.getlist("conf_name[]")
    masks = [form.getlist(f"conf_s{i}[]") for i in range(1, 7)]

    restored = []
    for idx, name in enumerate(names):
        row = {
            "group_name": groups[idx] if idx < len(groups) else "",
            "name": name,
        }
        for i in range(6):
            row[f"s{i + 1}"] = 1 if str(idx) in masks[i] else 0
        restored.append(row)
    return restored
