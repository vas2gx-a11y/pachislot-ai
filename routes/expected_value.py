from flask import Blueprint, render_template, request, redirect, url_for, flash

import common

expected_value_bp = Blueprint("expected_value", __name__, url_prefix="/expected_value")


def _to_float(form, name, default=0.0):
    raw = (form.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


@expected_value_bp.route("/")
def index():
    machine_name = request.args.get("machine_name", "")
    session_id = request.args.get("session_id", "")
    current_games_prefill = ""

    if session_id:
        records = [r for r in common.load_records() if str(r.get("session_id", "")) == session_id]
        if records:
            current_games_prefill = records[0].get("current_games", "")

    return render_template(
        "expected_value.html",
        machine_name=machine_name,
        session_id=session_id,
        current_games_prefill=current_games_prefill,
        form_values=None,
        result=None,
    )


@expected_value_bp.route("/calculate", methods=["POST"])
def calculate():
    machine_name = request.form.get("machine_name", "").strip()
    session_id = request.form.get("session_id", "").strip()

    current_games = _to_float(request.form, "current_games")
    target_games = _to_float(request.form, "target_games")
    coin_cost_per_game = _to_float(request.form, "coin_cost_per_game", 20.0)
    exchange_rate = _to_float(request.form, "exchange_rate")
    expected_payout_input = (request.form.get("expected_payout", "") or "").strip()
    use_ai_estimate = request.form.get("use_ai_estimate") == "on"

    if target_games <= 0 or exchange_rate <= 0:
        flash("狙い目G数と交換レートは必須です。0より大きい値を入力してください。")
        return render_template(
            "expected_value.html",
            machine_name=machine_name,
            session_id=session_id,
            current_games_prefill=current_games,
            form_values=request.form,
            result=None,
        )

    ai_note = ""
    if not expected_payout_input or use_ai_estimate:
        # 期待獲得枚数が未入力、または「AIに概算してもらう」が選択されている場合は
        # 機種スペックのゲームフローからAIに参考値を出してもらう(あくまで参考値)
        estimated, note = common.estimate_expected_payout_with_gemini(machine_name, target_games, current_games)
        ai_note = note
        expected_payout = estimated if estimated is not None else _to_float(request.form, "expected_payout")
    else:
        expected_payout = _to_float(request.form, "expected_payout")

    result = common.calculate_expected_value(
        current_games=current_games,
        target_games=target_games,
        coin_cost_per_game=coin_cost_per_game,
        expected_payout=expected_payout,
        exchange_rate=exchange_rate,
    )
    result["ai_note"] = ai_note

    return render_template(
        "expected_value.html",
        machine_name=machine_name,
        session_id=session_id,
        current_games_prefill=current_games,
        form_values=request.form,
        result=result,
    )
