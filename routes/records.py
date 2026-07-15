import base64
import json
import uuid
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash

import common

records_bp = Blueprint("records", __name__)

PAGE_SIZE = 30  # 一覧は最新分をこの件数ずつ表示する(件数が増えても表示が重くならないように)


@records_bp.route("/")
def index():
    preset_machine = request.args.get("machine_name", "")
    preset_session = request.args.get("session_id", "")

    all_history = common.load_records()  # 新しい順。統計やセッション検索など「全件」を扱う処理はこちらを使う
    dashboard_stats = common.build_dashboard_stats(all_history)

    # 一覧が空のときは「本当に未登録なのか、読み込みで何か問題が起きているのか」を
    # その場で確認できるよう、シートの生の状態も取得しておく
    sheet_diagnostics = common.get_records_sheet_diagnostics() if not all_history else None

    # ページネーション: 一度に描画するのは最新分だけに絞り、件数が増えても表示が重くならないようにする
    try:
        page = max(1, int(request.args.get("page", "1")))
    except (TypeError, ValueError):
        page = 1
    total_records = len(all_history)
    total_pages = max(1, (total_records + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(page, total_pages)
    start = (page - 1) * PAGE_SIZE
    history = all_history[start:start + PAGE_SIZE]  # 表示用(このページ分のみ)

    # 一覧の各項目から直接AIに質問できるように、セッションIDごとのQ&A履歴をまとめて取得しておく。
    # 表示しているページの記録に対応する分だけをグルーピングすればよいので、
    # 全件との突き合わせは省き、無駄な処理・レンダリング量を減らす。
    visible_session_ids = {str(r.get("session_id", "")) for r in history if r.get("session_id")}
    chat_history_by_session = {}
    for chat in common.load_all_chat_history():
        sid = str(chat.get("session_id", ""))
        if sid in visible_session_ids:
            chat_history_by_session.setdefault(sid, []).append(chat)

    # 「追加分析」等で機種名が指定されている場合、参考として使われる直近の同機種データをプレビュー表示する
    recent_history_preview = []
    hall_tendency_preview = ""
    preset_store = ""
    chat_history = []
    if preset_machine:
        _, preview_rule = common.find_machine_rule(preset_machine)
        # 継続セッションであれば、そのセッションで既に入力済みの店舗名を引き継いでプレビューに反映する。
        # 対象のセッションはページネーションで表示されていない古いページにある可能性があるため、
        # 必ず all_history(全件)から探す。
        if preset_session:
            for r in all_history:
                if str(r.get("session_id", "")) == preset_session and str(r.get("store_name", "")).strip():
                    preset_store = str(r.get("store_name", "")).strip()
                    break
            # このセッションのQ&A履歴も、表示ページに依存せず専用の読み込みで確実に取得する
            chat_history = common.load_chat_history(preset_session)
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
        page=page,
        total_pages=total_pages,
        total_records=total_records,
        sheet_diagnostics=sheet_diagnostics,
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
    max_diff, hamari_600, hamari_800, max_renchan = 0, 0, 0, 0
    graph_shape_tags = []
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
            max_diff = parsed_data.get("max_difference_slabs", 0) or 0
            hamari_600 = parsed_data.get("hamari_600_plus", 0) or 0
            hamari_800 = parsed_data.get("hamari_800_plus", 0) or 0
            max_renchan = parsed_data.get("max_renchan", 0) or 0
            raw_tags = parsed_data.get("graph_shape_tags") or []
            if isinstance(raw_tags, list):
                graph_shape_tags = [str(t).strip() for t in raw_tags if str(t).strip()]
            # 最大差枚が読み取れなかった場合は、最終差枚を下回らないはずなので保険として採用
            if max_diff <= 0:
                max_diff = diff
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
        "max_difference_slabs": max_diff,
        "hamari_600_plus": hamari_600,
        "hamari_800_plus": hamari_800,
        "max_renchan": max_renchan,
        "graph_shape_tags": graph_shape_tags,
    }

    # 今回の画像に台番号が写っていなかった場合、同一セッションの過去の記録から引き継ぐ
    if not machine_number and session_id:
        for r in common.load_records():
            if str(r.get("session_id", "")) == session_id and str(r.get("machine_number", "")).strip():
                machine_number = str(r.get("machine_number", "")).strip()
                break

    estimation_comment, setting_probabilities, category_scores = common.estimate(
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
        "category_scores": json.dumps(category_scores, ensure_ascii=False),
        "max_difference_slabs": max_diff,
        "hamari_600_plus": hamari_600,
        "hamari_800_plus": hamari_800,
        "max_renchan": max_renchan,
        "graph_shape_tags": ",".join(graph_shape_tags),
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
