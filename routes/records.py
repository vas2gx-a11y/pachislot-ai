import base64
import json
import uuid
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash

import common

records_bp = Blueprint("records", __name__)


@records_bp.route("/")
def index():
    preset_machine = request.args.get("machine_name", "")
    preset_session = request.args.get("session_id", "")
    history = common.load_records()
    dashboard_stats = common.build_dashboard_stats(history)

    # 一覧の各項目から直接AIに質問できるように、セッションIDごとのQ&A履歴をまとめて取得しておく
    # (履歴の件数分シートを読みに行くと遅くなるため、1回の読み込みでグルーピングする)
    chat_history_by_session = {}
    for chat in common.load_all_chat_history():
        sid = str(chat.get("session_id", ""))
        if sid:
            chat_history_by_session.setdefault(sid, []).append(chat)

    # 「追加分析」等で機種名が指定されている場合、参考として使われる直近の同機種データをプレビュー表示する
    recent_history_preview = []
    hall_tendency_preview = ""
    preset_store = ""
    chat_history = []
    if preset_machine:
        _, preview_rule = common.find_machine_rule(preset_machine)
        # 継続セッションであれば、そのセッションで既に入力済みの店舗名を引き継いでプレビューに反映する
        if preset_session:
            for r in history:
                if str(r.get("session_id", "")) == preset_session and str(r.get("store_name", "")).strip():
                    preset_store = str(r.get("store_name", "")).strip()
                    break
            chat_history = chat_history_by_session.get(preset_session, [])
        recent_history_preview = common.get_recent_same_machine_records(
            preset_machine, store_name=preset_store, exclude_session_id=preset_session, days=7
        )
        hall_tendency_preview = common.summarize_hall_tendency(
            recent_history_preview, hint_words=preview_rule.get("hint_words", []), store_filtered=bool(preset_store)
        )

    return render_template(
        "index.html",
        history=history,
        dashboard_stats=dashboard_stats,
        preset_machine=preset_machine,
        preset_session=preset_session,
        preset_store=preset_store,
        recent_history_preview=recent_history_preview,
        hall_tendency_preview=hall_tendency_preview,
        chat_history=chat_history,
        chat_history_by_session=chat_history_by_session,
    )


@records_bp.route("/upload", methods=["POST"])
def upload():
    machine_name = request.form.get("machine_name", "不明な機種").strip()
    store_name = request.form.get("store_name", "").strip()
    user_note = request.form.get("user_note", "").strip()
    session_id = request.form.get("session_id", "").strip()
    file = request.files.get("image")

    # 過去データをどれくらい遡って参考にするか(0=参照しない, 1=前日のみ, 7=今週, 30=今月)
    try:
        history_days = int(request.form.get("history_days", "7"))
    except (TypeError, ValueError):
        history_days = 7
    history_days = max(0, min(history_days, 90))

    # session_id が無ければ、これは新しい台のセッションとして新規発行
    if not session_id:
        session_id = uuid.uuid4().hex[:12]

    # 登録済みの機種データ(強示唆ワード・ゲームフロー)を先に取得し、画像解析にも活用する
    _, machine_rule = common.find_machine_rule(machine_name)
    machine_hint_words = machine_rule.get("hint_words", [])
    machine_game_flow = machine_rule.get("game_flow", "")

    total, big, reg, current, diff = 0, 0, 0, 0, 0
    machine_number = ""
    graph_features, other_info = "画像なし", "特になし"
    # 設定予測(estimate)にも同じ画像を渡し、グラフの形状などを直接判定材料にする
    image_for_estimate = None
    mime_type_for_estimate = "image/jpeg"

    if file and file.filename != "":
        if not common.allowed_file(file.filename):
            flash("対応していないファイル形式です(jpg / jpeg / png / webp のみ)")
            return redirect(url_for("records.index", machine_name=machine_name, session_id=session_id))

        ext = file.filename.rsplit(".", 1)[1].lower()
        mime_type = "image/png" if ext == "png" else "image/webp" if ext == "webp" else "image/jpeg"

        image_bytes = file.read()
        base64_image = base64.b64encode(image_bytes).decode("utf-8")
        image_for_estimate = base64_image
        mime_type_for_estimate = mime_type
        parsed_data = common.analyze_image_with_gemini(
            base64_image, mime_type,
            machine_name=machine_name,
            hint_words=machine_hint_words,
            game_flow=machine_game_flow,
        )

        if parsed_data:
            total = parsed_data.get("total_games", 0)
            big = parsed_data.get("big_count", 0)
            reg = parsed_data.get("reg_count", 0)
            current = parsed_data.get("current_games", 0)
            diff = parsed_data.get("difference_slabs", 0)
            machine_number = str(parsed_data.get("machine_number", "") or "").strip()
            graph_features = parsed_data.get("graph_features", "不明")
            other_info = parsed_data.get("other_info", "特になし")
        else:
            flash("画像の解析に失敗しました。手動で確認してください。")
            graph_features, other_info = "解析失敗", "解析失敗"

    # 今回の入力に店舗名が無かった場合、同一セッションの過去の記録から引き継ぐ
    # (店舗名でホールの傾向分析を絞り込むため、参考データ取得の前に確定させておく)
    if not store_name and session_id:
        for r in common.load_records():
            if str(r.get("session_id", "")) == session_id and str(r.get("store_name", "")).strip():
                store_name = str(r.get("store_name", "")).strip()
                break

    # このセッションの過去のメモ・AI備考も合わせて、設定予測をやり直す
    past_text = common.get_session_history_text(session_id)
    combined_text = " ".join([past_text, user_note, graph_features, other_info])

    # 前日・今週など、同機種の別セッションの記録も参考情報として取得する
    if history_days > 0:
        recent_records = common.get_recent_same_machine_records(
            machine_name, store_name=store_name, exclude_session_id=session_id, days=history_days
        )
        recent_history_text = common.format_recent_history(recent_records)
        hall_tendency_text = common.summarize_hall_tendency(
            recent_records, hint_words=machine_hint_words, store_filtered=bool(store_name)
        )
    else:
        recent_records = []
        recent_history_text = "参照しない設定のため未参照"
        hall_tendency_text = "参照しない設定のため未算出"

    stats = {
        "total_games": total,
        "big_count": big,
        "reg_count": reg,
        "current_games": current,
        "difference_slabs": diff,
    }

    # 今回の画像に台番号が写っていなかった場合、同一セッションの過去の記録から引き継ぐ
    if not machine_number and session_id:
        for r in common.load_records():
            if str(r.get("session_id", "")) == session_id and str(r.get("machine_number", "")).strip():
                machine_number = str(r.get("machine_number", "")).strip()
                break

    estimation_comment, setting_probabilities = common.estimate(
        machine_name, combined_text, stats, recent_history_text, hall_tendency_text,
        recent_records_count=len(recent_records),
        base64_image=image_for_estimate, mime_type=mime_type_for_estimate,
    )

    record = {
        "session_id": session_id,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "machine_name": machine_name,
        "machine_number": machine_number,
        "store_name": store_name,
        "total_games": total,
        "big_count": big,
        "reg_count": reg,
        "current_games": current,
        "difference_slabs": diff,
        "graph_features": graph_features,
        "other_info": other_info,
        "user_note": user_note,
        "estimation": estimation_comment,
        "setting_probabilities": json.dumps(setting_probabilities, ensure_ascii=False),
    }
    common.save_record(record)
    return redirect(url_for("records.index"))


@records_bp.route("/ask", methods=["POST"])
def ask():
    session_id = request.form.get("session_id", "").strip()
    machine_name = request.form.get("machine_name", "").strip()
    question = request.form.get("question", "").strip()

    if not session_id:
        flash("セッションが見つかりません。まずデータを登録してください。")
        return redirect(url_for("records.index"))

    if not question:
        flash("質問内容を入力してください。")
        return redirect(url_for("records.index", machine_name=machine_name, session_id=session_id))

    # これまでのQ&A履歴を文脈として渡すため取得しておく
    past_chat = common.load_chat_history(session_id)
    chat_history_pairs = [(r.get("question", ""), r.get("answer", "")) for r in past_chat]

    answer = common.answer_question(session_id, machine_name, question, chat_history=chat_history_pairs)
    common.save_chat_message(session_id, question, answer)

    return redirect(url_for("records.index", machine_name=machine_name, session_id=session_id))


@records_bp.route("/machine_chart")
def machine_chart():
    store_name = request.args.get("store_name", "").strip()
    machine_number = request.args.get("machine_number", "").strip()
    machine_name = request.args.get("machine_name", "").strip()

    if not store_name or not machine_number:
        flash("店舗名と台番号の両方が入力されている記録のみ、グラフ表示できます。")
        return redirect(url_for("records.index"))

    history = common.get_store_machine_history(store_name, machine_number, days=90)

    chart_labels = [str(r.get("date", ""))[:10] for r in history]
    chart_games = [r.get("total_games", 0) for r in history]
    chart_diffs = [r.get("difference_slabs", 0) for r in history]

    return render_template(
        "machine_chart.html",
        store_name=store_name,
        machine_number=machine_number,
        machine_name=machine_name,
        history=history,
        chart_labels=chart_labels,
        chart_games=chart_games,
        chart_diffs=chart_diffs,
    )
