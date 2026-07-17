import base64

from flask import Blueprint, render_template, request, redirect, url_for, flash

import common

machines_bp = Blueprint("machines", __name__, url_prefix="/machines")


def _build_machine_list():
    rules = common.load_machine_rules()
    return [
        {
            "keyword": keyword,
            "hint_words": rule.get("hint_words", []),
            "game_flow": rule.get("game_flow", ""),
            "setting_ratios": rule.get("setting_ratios", {}),
            "sources": rule.get("sources", []),
            "suggestion_items": rule.get("suggestion_items", []),
        }
        for keyword, rule in rules.items()
    ]


def _resolve_keyword_for_merge(manual_keyword, ai_machine_name, rules):
    """
    保存先のキーワードを決定する。
    手入力のキーワードがあればそれを最優先(ユーザーの意思を尊重)。
    無ければAIが読み取った機種名を使うが、その前に既存の登録キーワードと
    実質同じ機種を指していないか確認し、該当すれば既存キーワードを再利用する
    (表記ゆれによってデータが複数キーワードに分裂し、集約されなくなるのを防ぐため)。
    """
    if manual_keyword:
        return manual_keyword
    merge_target = common.find_mergeable_keyword(ai_machine_name, rules)
    return merge_target or ai_machine_name


@machines_bp.route("/")
def machines_page():
    machine_list = _build_machine_list()
    # 一覧が空のときは「本当に未登録なのか、読み込みで何か問題が起きているのか」を
    # その場で確認できるよう、シートの生の状態も取得しておく
    sheet_diagnostics = common.get_machines_sheet_diagnostics() if not machine_list else None

    return render_template(
        "machines.html",
        machine_list=machine_list,
        spreadsheet_url=common.SPREADSHEET_URL,
        machines_sheet_name=common.MACHINES_SHEET_NAME,
        sheet_diagnostics=sheet_diagnostics,
    )


@machines_bp.route("/add_note", methods=["POST"])
def add_note():
    keyword = request.form.get("keyword", "").strip()
    note = request.form.get("note", "").strip()

    if not keyword:
        flash("対象の機種が特定できませんでした。")
        return redirect(url_for("machines.machines_page"))

    if not note:
        flash("追記するメモを入力してください。")
        return redirect(url_for("machines.machines_page"))

    if common.add_machine_note(keyword, note):
        flash(f"「{keyword}」にメモを追記しました。")
    else:
        flash("メモの追記に失敗しました。")

    return redirect(url_for("machines.machines_page"))


@machines_bp.route("/add_suggestion_item", methods=["POST"])
def add_suggestion_item():
    keyword = request.form.get("keyword", "").strip()
    name = request.form.get("item_name", "").strip()
    item_type = request.form.get("item_type", "count").strip()
    weight_raw = request.form.get("weight", "0").strip()

    if not keyword:
        flash("対象の機種が特定できませんでした。")
        return redirect(url_for("machines.machines_page"))

    if not name:
        flash("示唆項目の名前を入力してください。")
        return redirect(url_for("machines.machines_page"))

    try:
        weight = int(weight_raw)
    except ValueError:
        weight = 0

    if common.add_suggestion_item(keyword, name, item_type, weight):
        flash(f"「{keyword}」に示唆項目「{name}」を登録しました。")
    else:
        flash("示唆項目の登録に失敗しました。")

    return redirect(url_for("machines.machines_page"))


@machines_bp.route("/remove_suggestion_item", methods=["POST"])
def remove_suggestion_item():
    keyword = request.form.get("keyword", "").strip()
    name = request.form.get("item_name", "").strip()

    if not keyword or not name:
        flash("削除対象を特定できませんでした。")
        return redirect(url_for("machines.machines_page"))

    if common.remove_suggestion_item(keyword, name):
        flash(f"「{keyword}」の示唆項目「{name}」を削除しました。")
    else:
        flash("示唆項目の削除に失敗しました。")

    return redirect(url_for("machines.machines_page"))


@machines_bp.route("/upload", methods=["POST"])
def machines_upload():
    manual_keyword = request.form.get("keyword", "").strip()
    manual_hint_words_raw = request.form.get("hint_words", "").strip()
    manual_hint_words = [w.strip() for w in manual_hint_words_raw.split(",") if w.strip()]
    file = request.files.get("spec_image")

    if not file or file.filename == "":
        flash("スペック画像を選択してください。")
        return redirect(url_for("machines.machines_page"))

    if not common.allowed_file(file.filename):
        flash("対応していないファイル形式です(jpg / jpeg / png / webp のみ)")
        return redirect(url_for("machines.machines_page"))

    ext = file.filename.rsplit(".", 1)[1].lower()
    mime_type = "image/png" if ext == "png" else "image/webp" if ext == "webp" else "image/jpeg"

    image_bytes = file.read()
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    parsed = common.analyze_machine_spec_with_gemini(base64_image, mime_type)

    if not parsed:
        flash("スペック画像の解析に失敗しました。もう一度お試しください。")
        return redirect(url_for("machines.machines_page"))

    ai_machine_name = str(parsed.get("machine_name", "")).strip()
    rules = common.load_machine_rules()
    keyword = _resolve_keyword_for_merge(manual_keyword, ai_machine_name, rules)
    if not keyword:
        flash("機種名を読み取れませんでした。機種名キーワードを手入力してください。")
        return redirect(url_for("machines.machines_page"))

    ai_hint_words = parsed.get("hint_words") or []
    if not isinstance(ai_hint_words, list):
        ai_hint_words = []
    # 手入力の強示唆ワードとAI抽出分を合算(重複除去)
    combined_hint_words = list(dict.fromkeys(manual_hint_words + [str(w).strip() for w in ai_hint_words if str(w).strip()]))

    game_flow = str(parsed.get("game_flow", "")).strip()
    setting_ratios = parsed.get("setting_ratios") or {}
    if not isinstance(setting_ratios, dict):
        setting_ratios = {}

    source_label = f"画像アップロード: {file.filename}"
    if common.save_machine_rule(keyword, combined_hint_words, game_flow, setting_ratios, source_label=source_label):
        if keyword != ai_machine_name and ai_machine_name:
            flash(f"「{ai_machine_name}」を、既存の「{keyword}」に統合して登録しました。")
        else:
            flash(f"「{keyword}」の機種データを登録しました。")
    else:
        flash("機種データの保存に失敗しました。")

    return redirect(url_for("machines.machines_page"))


@machines_bp.route("/import_url", methods=["POST"])
def machines_import_url():
    spec_url = request.form.get("spec_url", "").strip()
    manual_keyword = request.form.get("url_keyword", "").strip()
    manual_hint_words_raw = request.form.get("url_hint_words", "").strip()
    manual_hint_words = [w.strip() for w in manual_hint_words_raw.split(",") if w.strip()]

    if not spec_url:
        flash("取り込み元のURLを入力してください。")
        return redirect(url_for("machines.machines_page"))

    if not (spec_url.startswith("http://") or spec_url.startswith("https://")):
        flash("URLは http:// または https:// から始まる形式で入力してください。")
        return redirect(url_for("machines.machines_page"))

    page_text = common.fetch_url_text(spec_url)
    if not page_text:
        flash("ページの取得に失敗しました。URLが正しいか、公開ページかどうかをご確認ください。")
        return redirect(url_for("machines.machines_page"))

    parsed = common.analyze_machine_url_with_gemini(page_text, source_url=spec_url)
    if not parsed:
        flash("ページ内容の解析に失敗しました。もう一度お試しください。")
        return redirect(url_for("machines.machines_page"))

    ai_machine_name = str(parsed.get("machine_name", "")).strip()
    rules = common.load_machine_rules()
    keyword = _resolve_keyword_for_merge(manual_keyword, ai_machine_name, rules)
    if not keyword:
        flash("機種名を読み取れませんでした。機種名キーワードを手入力してください。")
        return redirect(url_for("machines.machines_page"))

    ai_hint_words = parsed.get("hint_words") or []
    if not isinstance(ai_hint_words, list):
        ai_hint_words = []
    # 手入力の強示唆ワードとAI抽出分を合算(重複除去)
    combined_hint_words = list(dict.fromkeys(manual_hint_words + [str(w).strip() for w in ai_hint_words if str(w).strip()]))

    game_flow = str(parsed.get("game_flow", "")).strip()
    setting_ratios = parsed.get("setting_ratios") or {}
    if not isinstance(setting_ratios, dict):
        setting_ratios = {}

    if common.save_machine_rule(keyword, combined_hint_words, game_flow, setting_ratios, source_label=spec_url):
        if keyword != ai_machine_name and ai_machine_name:
            flash(f"「{ai_machine_name}」を、既存の「{keyword}」に統合してURLから取り込みました。")
        else:
            flash(f"「{keyword}」の機種データをURLから取り込みました。")
    else:
        flash("機種データの保存に失敗しました。")

    return redirect(url_for("machines.machines_page"))


@machines_bp.route("/diagnose", methods=["GET"])
def diagnose():
    machine_name = request.args.get("machine_name", "")
    diagnosis = common.debug_machine_name_match(machine_name) if machine_name else None
    machine_list = _build_machine_list()
    sheet_diagnostics = common.get_machines_sheet_diagnostics() if not machine_list else None

    return render_template(
        "machines.html",
        machine_list=machine_list,
        spreadsheet_url=common.SPREADSHEET_URL,
        machines_sheet_name=common.MACHINES_SHEET_NAME,
        diagnosis=diagnosis,
        diagnose_input=machine_name,
        sheet_diagnostics=sheet_diagnostics,
    )
