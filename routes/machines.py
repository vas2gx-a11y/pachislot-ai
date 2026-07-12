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
            "basic_info": rule.get("basic_info", {}),
            "game_flow": rule.get("game_flow", ""),
            "setting_ratios": rule.get("setting_ratios", {}),
            "ceiling_info": rule.get("ceiling_info", {}),
            "bonus_info": rule.get("bonus_info", {}),
            "cz_info": rule.get("cz_info", {}),
            "suggestion_info": rule.get("suggestion_info", {}),
            "extra_info": rule.get("extra_info", ""),
            "source_url": rule.get("source_url", ""),
        }
        for keyword, rule in rules.items()
    ]

    return render_template(
        "machines.html",
        machine_list=machine_list,
        spreadsheet_url=common.SPREADSHEET_URL,
        machines_sheet_name=common.MACHINES_SHEET_NAME,
    )


def _extract_fields_from_parsed(parsed):
    """Geminiの解析結果(dict)を save_machine_rule に渡す fields dict に変換する"""
    def _as_dict(value):
        return value if isinstance(value, dict) else {}

    hint_words = parsed.get("hint_words") or []
    if not isinstance(hint_words, list):
        hint_words = []

    return {
        "hint_words": [str(w).strip() for w in hint_words if str(w).strip()],
        "game_flow": str(parsed.get("game_flow", "")).strip(),
        "basic_info": _as_dict(parsed.get("basic_info")),
        "setting_ratios": _as_dict(parsed.get("setting_ratios")),
        "ceiling_info": _as_dict(parsed.get("ceiling_info")),
        "bonus_info": _as_dict(parsed.get("bonus_info")),
        "cz_info": _as_dict(parsed.get("cz_info")),
        "suggestion_info": _as_dict(parsed.get("suggestion_info")),
        "extra_info": str(parsed.get("extra_info", "")).strip(),
        "source_url": str(parsed.get("source_url", "")).strip(),
    }


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

    fields = _extract_fields_from_parsed(parsed)
    # 手入力の強示唆ワードを画像解析分と合算
    fields["hint_words"] = list(dict.fromkeys(manual_hint_words + fields["hint_words"]))

    if common.save_machine_rule(keyword, fields):
        flash(f"「{keyword}」の機種データを登録しました。")
    else:
        flash("機種データの保存に失敗しました。")

    return redirect(url_for("machines.machines_page"))


@machines_bp.route("/import_url", methods=["POST"])
def machines_import_url():
    spec_url = request.form.get("spec_url", "").strip()
    manual_keyword = request.form.get("url_keyword", "").strip()

    if not spec_url:
        flash("URLを入力してください。")
        return redirect(url_for("machines.machines_page"))
    if not (spec_url.startswith("http://") or spec_url.startswith("https://")):
        flash("正しいURL(http:// または https:// から始まるもの)を入力してください。")
        return redirect(url_for("machines.machines_page"))

    page_text = common.fetch_page_text(spec_url)
    if not page_text:
        flash("ページの取得に失敗しました。URLをご確認ください。")
        return redirect(url_for("machines.machines_page"))

    parsed = common.analyze_machine_spec_from_text(page_text, source_url=spec_url)
    if not parsed:
        flash("スペック情報の解析に失敗しました。もう一度お試しください。")
        return redirect(url_for("machines.machines_page"))

    keyword = manual_keyword or str(parsed.get("machine_name", "")).strip()
    if not keyword:
        flash("機種名を読み取れませんでした。機種名キーワードを手入力してください。")
        return redirect(url_for("machines.machines_page"))

    fields = _extract_fields_from_parsed(parsed)

    if common.save_machine_rule(keyword, fields):
        flash(f"「{keyword}」の機種データをURLから取り込みました。")
    else:
        flash("機種データの保存に失敗しました。")

    return redirect(url_for("machines.machines_page"))
