import base64

from flask import Blueprint, render_template, request, redirect, url_for, flash

import common

machines_bp = Blueprint("machines", __name__, url_prefix="/machines")


@machines_bp.route("/")
def machines_page():
    rules = common.load_machine_rules()
    machine_list = [
        {
            "keyword": keyword,
            "hint_words": rule.get("hint_words", []),
            "game_flow": rule.get("game_flow", ""),
            "setting_ratios": rule.get("setting_ratios", {}),
        }
        for keyword, rule in rules.items()
    ]

    return render_template(
        "machines.html",
        machine_list=machine_list,
        spreadsheet_url=common.SPREADSHEET_URL,
        machines_sheet_name=common.MACHINES_SHEET_NAME,
    )


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

    keyword = manual_keyword or str(parsed.get("machine_name", "")).strip()
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

    if common.save_machine_rule(keyword, combined_hint_words, game_flow, setting_ratios):
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

    keyword = manual_keyword or str(parsed.get("machine_name", "")).strip()
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

    if common.save_machine_rule(keyword, combined_hint_words, game_flow, setting_ratios):
        flash(f"「{keyword}」の機種データをURLから取り込みました。")
    else:
        flash("機種データの保存に失敗しました。")

    return redirect(url_for("machines.machines_page"))
