import os
import base64
import json
import logging
import uuid
from datetime import datetime

import requests
import gspread
from google.oauth2.service_account import Credentials
from flask import Flask, render_template, request, redirect, url_for, flash

# --- ロギング設定 ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))

# --- Gemini設定 ---
API_KEY = os.environ.get("GEMINI_API_KEY")
if not API_KEY:
    raise RuntimeError(
        "環境変数 GEMINI_API_KEY が設定されていません。"
        "実行前に `export GEMINI_API_KEY=あなたのキー` を行ってください。"
    )

MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={API_KEY}"
REQUEST_TIMEOUT = 30  # seconds

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}
MAX_UPLOAD_SIZE = 8 * 1024 * 1024  # 8MB
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_SIZE

# --- Googleスプレッドシート設定 ---
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID")
SHEET_NAME = os.environ.get("SHEET_NAME", "records")
MACHINES_SHEET_NAME = os.environ.get("MACHINES_SHEET_NAME", "machines")
SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

if not SPREADSHEET_ID:
    raise RuntimeError("環境変数 SPREADSHEET_ID が設定されていません。")
if not SERVICE_ACCOUNT_JSON:
    raise RuntimeError("環境変数 GOOGLE_SERVICE_ACCOUNT_JSON が設定されていません。")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# records シートの列構成(session_idを追加)
HEADERS = [
    "session_id", "date", "machine_name", "total_games", "big_count", "reg_count",
    "current_games", "difference_slabs", "graph_features",
    "other_info", "user_note", "estimation",
]

# machines シートの列構成(機種名キーワード → 強示唆ワード群)
MACHINE_HEADERS = ["keyword", "hint_words"]

# 初回起動時、machinesシートが空だった場合に入れておくデフォルト値
DEFAULT_MACHINE_RULES = [
    {"keyword": "ToLOVE", "hint_words": "強示唆,高確,チャンス"},
    {"keyword": "トラブル", "hint_words": "強示唆,高確,チャンス"},
]


# ---------------------------------------------------------------------------
# Googleスプレッドシート接続
# ---------------------------------------------------------------------------
def get_client():
    creds_dict = json.loads(SERVICE_ACCOUNT_JSON)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)


def get_records_worksheet():
    client = get_client()
    sheet = client.open_by_key(SPREADSHEET_ID)
    try:
        ws = sheet.worksheet(SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=SHEET_NAME, rows=1000, cols=len(HEADERS))
        ws.append_row(HEADERS)
    if ws.row_values(1) != HEADERS:
        ws.insert_row(HEADERS, 1)
    return ws


def get_machines_worksheet():
    client = get_client()
    sheet = client.open_by_key(SPREADSHEET_ID)
    try:
        ws = sheet.worksheet(MACHINES_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=MACHINES_SHEET_NAME, rows=200, cols=len(MACHINE_HEADERS))
        ws.append_row(MACHINE_HEADERS)
        for rule in DEFAULT_MACHINE_RULES:
            ws.append_row([rule["keyword"], rule["hint_words"]])
    if ws.row_values(1) != MACHINE_HEADERS:
        ws.insert_row(MACHINE_HEADERS, 1)
    return ws


NUMERIC_FIELDS = ["total_games", "big_count", "reg_count", "current_games", "difference_slabs"]


def _to_int(value):
    """スプレッドシートのセルが空文字や文字列で返ってきても安全にintへ変換する"""
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def load_records():
    try:
        ws = get_records_worksheet()
        records = ws.get_all_records()
        for r in records:
            for field in NUMERIC_FIELDS:
                r[field] = _to_int(r.get(field, 0))
        records.reverse()  # 新しい順に表示
        return records
    except Exception as e:
        logger.error(f"スプレッドシート読み込みエラー: {e}")
        return []


def save_record(record):
    try:
        ws = get_records_worksheet()
        row = [record.get(h, "") for h in HEADERS]
        ws.append_row(row)
    except Exception as e:
        logger.error(f"スプレッドシート書き込みエラー: {e}")
        flash("スプレッドシートへの保存に失敗しました。")


def load_machine_rules():
    """machines シートから {keyword: [hint_words...]} の辞書を作る"""
    try:
        ws = get_machines_worksheet()
        rows = ws.get_all_records()
        rules = {}
        for row in rows:
            keyword = str(row.get("keyword", "")).strip()
            hint_words_raw = str(row.get("hint_words", "")).strip()
            if not keyword:
                continue
            hint_words = [w.strip() for w in hint_words_raw.split(",") if w.strip()]
            rules[keyword] = hint_words
        return rules
    except Exception as e:
        logger.error(f"機種マスタ読み込みエラー: {e}")
        return {}


def get_session_history_text(session_id):
    """同じセッションの過去のメモ・AI備考を全部つなげたテキストを返す"""
    if not session_id:
        return ""
    records = load_records()
    texts = []
    for r in records:
        if str(r.get("session_id", "")) == session_id:
            texts.append(str(r.get("user_note", "")))
            texts.append(str(r.get("graph_features", "")))
            texts.append(str(r.get("other_info", "")))
    return " ".join(texts)


# ---------------------------------------------------------------------------
# 画像解析 (Gemini)
# ---------------------------------------------------------------------------
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def analyze_image_with_gemini(base64_image, mime_type="image/jpeg"):
    prompt = """
    パチスロのデータ画面です。以下のJSON形式でのみ出力してください。他の文章は不要です。
    {"total_games": 0, "big_count": 0, "reg_count": 0, "current_games": 0, "difference_slabs": 0, "graph_features": "", "other_info": ""}
    """
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {"inlineData": {"mimeType": mime_type, "data": base64_image}},
                ]
            }
        ]
    }
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.post(
            GEMINI_URL, headers=headers, data=json.dumps(payload), timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
    except requests.exceptions.Timeout:
        logger.error("Gemini API タイムアウト")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Gemini API 通信エラー: {e}")
        return None

    try:
        raw_text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        json_start = raw_text.find("{")
        json_end = raw_text.rfind("}") + 1
        if json_start == -1 or json_end == 0:
            logger.error(f"JSONが見つかりません: {raw_text}")
            return None
        return json.loads(raw_text[json_start:json_end])
    except (KeyError, IndexError) as e:
        logger.error(f"Geminiレスポンス構造エラー: {e}")
    except json.JSONDecodeError as e:
        logger.error(f"JSON解析エラー: {e} / raw={raw_text!r}")
    return None


# ---------------------------------------------------------------------------
# 推測ロジック
# ---------------------------------------------------------------------------
def estimate(machine_name, combined_text):
    """
    machine_name に一致するキーワードを machines シートから探し、
    combined_text(このセッションの全メモ+AI備考をまとめたもの)の中に
    強示唆ワードが含まれているかで判定する。
    """
    rules = load_machine_rules()
    for keyword, hint_words in rules.items():
        if keyword and keyword in machine_name:
            if hint_words and any(hint in combined_text for hint in hint_words):
                return "高設定濃厚!? (要確認)"
            return "推測中..."
    return "通常・展開次第"


# ---------------------------------------------------------------------------
# ルート
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    preset_machine = request.args.get("machine_name", "")
    preset_session = request.args.get("session_id", "")
    history = load_records()
    return render_template(
        "index.html",
        history=history,
        preset_machine=preset_machine,
        preset_session=preset_session,
    )


@app.route("/upload", methods=["POST"])
def upload():
    machine_name = request.form.get("machine_name", "不明な機種").strip()
    user_note = request.form.get("user_note", "").strip()
    session_id = request.form.get("session_id", "").strip()
    file = request.files.get("image")

    # session_id が無ければ、これは新しい台のセッションとして新規発行
    if not session_id:
        session_id = uuid.uuid4().hex[:12]

    total, big, reg, current, diff = 0, 0, 0, 0, 0
    graph_features, other_info = "画像なし", "特になし"

    if file and file.filename != "":
        if not allowed_file(file.filename):
            flash("対応していないファイル形式です(jpg / jpeg / png / webp のみ)")
            return redirect(url_for("index", machine_name=machine_name, session_id=session_id))

        ext = file.filename.rsplit(".", 1)[1].lower()
        mime_type = "image/png" if ext == "png" else "image/webp" if ext == "webp" else "image/jpeg"

        image_bytes = file.read()
        base64_image = base64.b64encode(image_bytes).decode("utf-8")
        parsed_data = analyze_image_with_gemini(base64_image, mime_type)

        if parsed_data:
            total = parsed_data.get("total_games", 0)
            big = parsed_data.get("big_count", 0)
            reg = parsed_data.get("reg_count", 0)
            current = parsed_data.get("current_games", 0)
            diff = parsed_data.get("difference_slabs", 0)
            graph_features = parsed_data.get("graph_features", "不明")
            other_info = parsed_data.get("other_info", "特になし")
        else:
            flash("画像の解析に失敗しました。手動で確認してください。")
            graph_features, other_info = "解析失敗", "解析失敗"

    # このセッションの過去のメモ・AI備考も合わせて、設定予測をやり直す
    past_text = get_session_history_text(session_id)
    combined_text = " ".join([past_text, user_note, graph_features, other_info])

    record = {
        "session_id": session_id,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "machine_name": machine_name,
        "total_games": total,
        "big_count": big,
        "reg_count": reg,
        "current_games": current,
        "difference_slabs": diff,
        "graph_features": graph_features,
        "other_info": other_info,
        "user_note": user_note,
        "estimation": estimate(machine_name, combined_text),
    }
    save_record(record)
    return redirect(url_for("index"))


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=debug_mode)
