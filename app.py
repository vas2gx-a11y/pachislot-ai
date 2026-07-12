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

# machinesシートに直接テキストを書き込みたい場合のためのスプレッドシート直接リンク
SPREADSHEET_URL = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"

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

# machines シートの列構成
# keyword: 機種名に含まれるキーワード
# hint_words: 強示唆ワード群(カンマ区切り)
# game_flow: ゲームフロー・システムの説明(AT/ART純増、上乗せ契機など)
# setting_ratios: 設定1〜6ごとの確率(BIG/REG/合成など)をJSON文字列で格納
MACHINE_HEADERS = ["keyword", "hint_words", "game_flow", "setting_ratios"]

# 初回起動時、machinesシートが空だった場合に入れておくデフォルト値
DEFAULT_MACHINE_RULES = [
    {"keyword": "ToLOVE", "hint_words": "強示唆,高確,チャンス", "game_flow": "", "setting_ratios": "{}"},
    {"keyword": "トラブル", "hint_words": "強示唆,高確,チャンス", "game_flow": "", "setting_ratios": "{}"},
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
            ws.append_row([
                rule["keyword"], rule["hint_words"],
                rule.get("game_flow", ""), rule.get("setting_ratios", "{}"),
            ])
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
    """
    machines シートから
    {keyword: {"hint_words": [...], "game_flow": "...", "setting_ratios": {...}}}
    の辞書を作る
    """
    try:
        ws = get_machines_worksheet()
        rows = ws.get_all_records()
        rules = {}
        for row in rows:
            keyword = str(row.get("keyword", "")).strip()
            if not keyword:
                continue
            hint_words_raw = str(row.get("hint_words", "")).strip()
            hint_words = [w.strip() for w in hint_words_raw.split(",") if w.strip()]
            game_flow = str(row.get("game_flow", "")).strip()
            setting_ratios_raw = str(row.get("setting_ratios", "")).strip()
            if setting_ratios_raw:
                try:
                    setting_ratios = json.loads(setting_ratios_raw)
                except json.JSONDecodeError:
                    # JSON形式でなければ、スプレッドシートに直接書かれた自由記述テキストとして扱う
                    setting_ratios = setting_ratios_raw
            else:
                setting_ratios = {}
            rules[keyword] = {
                "hint_words": hint_words,
                "game_flow": game_flow,
                "setting_ratios": setting_ratios,
            }
        return rules
    except Exception as e:
        logger.error(f"機種マスタ読み込みエラー: {e}")
        return {}


def save_machine_rule(keyword, hint_words, game_flow, setting_ratios):
    """
    machines シートに機種情報を保存する。
    同じ keyword の行が既にあれば上書き、なければ新規追加する(upsert)。
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return False
    hint_words_str = ",".join(w.strip() for w in hint_words if w.strip())
    setting_ratios_json = json.dumps(setting_ratios or {}, ensure_ascii=False)

    try:
        ws = get_machines_worksheet()
        existing_keywords = ws.col_values(1)  # 1列目 = keyword
        target_row = None
        for i, value in enumerate(existing_keywords[1:], start=2):  # ヘッダー行を除く
            if str(value).strip() == keyword:
                target_row = i
                break

        row_values = [keyword, hint_words_str, game_flow, setting_ratios_json]
        if target_row:
            ws.update(f"A{target_row}:D{target_row}", [row_values])
        else:
            ws.append_row(row_values)
        return True
    except Exception as e:
        logger.error(f"機種マスタ書き込みエラー: {e}")
        return False


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


def get_recent_same_machine_records(machine_name, exclude_session_id="", days=7, limit=5):
    """
    同じ機種名(前日・今週など、別セッションを含む)の直近の記録を取得する。
    現在編集中のセッション(exclude_session_id)は除外し、日付が新しい順に最大limit件返す。
    ※ セッション単位で最新1件のみを採用する(同じ来店で何度も記録した分の重複を避けるため)。
    """
    machine_name = (machine_name or "").strip()
    if not machine_name:
        return []

    records = load_records()  # 新しい順
    now = datetime.now()
    seen_sessions = set()
    matched = []

    for r in records:
        sid = str(r.get("session_id", ""))
        if sid and sid == exclude_session_id:
            continue
        r_machine = str(r.get("machine_name", "")).strip()
        if not r_machine:
            continue
        # 機種名が部分一致していれば同じ機種とみなす(表記ゆれをある程度許容)
        if machine_name not in r_machine and r_machine not in machine_name:
            continue
        try:
            record_date = datetime.strptime(str(r.get("date", "")), "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        if (now - record_date).days > days:
            continue
        if sid in seen_sessions:
            continue  # 同一セッションは最新(=最初に出てくる)1件のみ採用
        seen_sessions.add(sid)
        matched.append(r)
        if len(matched) >= limit:
            break

    return matched


def format_recent_history(records):
    """get_recent_same_machine_records() の結果を、AIプロンプト用の読みやすいテキストに変換する"""
    if not records:
        return "登録なし"
    lines = []
    for r in records:
        note = str(r.get("user_note", "")).strip() or "特になし"
        lines.append(
            f"{r.get('date', '')}: 総回転数{r.get('total_games', 0)}G, "
            f"BIG{r.get('big_count', 0)}回, REG{r.get('reg_count', 0)}回, "
            f"差枚{r.get('difference_slabs', 0)}枚, メモ:{note}"
        )
    return " / ".join(lines)


def summarize_hall_tendency(records, hint_words=None):
    """
    直近の同機種データから、そのホール・その台の実際の傾向(平均差枚・勝率・
    強示唆ワードの出現頻度など)を集計し、判定材料として使えるサマリー文を作る。
    """
    if not records:
        return "傾向データなし(過去データが登録されていないため判定不可)"

    hint_words = hint_words or []
    n = len(records)
    diffs = [r.get("difference_slabs", 0) for r in records]
    avg_diff = sum(diffs) / n
    plus_count = sum(1 for d in diffs if d > 0)
    plus_rate = plus_count / n * 100

    hint_hit_count = 0
    if hint_words:
        for r in records:
            note_text = f"{r.get('user_note', '')} {r.get('graph_features', '')} {r.get('other_info', '')}"
            if any(hint in note_text for hint in hint_words):
                hint_hit_count += 1

    summary = (
        f"直近{n}回の平均差枚: {avg_diff:+.0f}枚, "
        f"プラス収支の割合: {plus_rate:.0f}% ({plus_count}/{n}回)"
    )
    if hint_words:
        summary += f", 強示唆ワード出現: {hint_hit_count}/{n}回"

    return summary


# ---------------------------------------------------------------------------
# 画像解析 (Gemini)
# ---------------------------------------------------------------------------
def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def analyze_image_with_gemini(base64_image, mime_type="image/jpeg", machine_name="", hint_words=None, game_flow=""):
    hint_words = hint_words or []
    machine_context = ""
    if machine_name:
        machine_context = f"""
    この画像は「{machine_name}」のデータ画面(または関連する画面)です。
    この機種で登録されている強示唆ワード: {", ".join(hint_words) if hint_words else "登録なし"}
    この機種のゲームフロー: {game_flow if game_flow else "登録なし"}
    画像内の文字・演出・グラフに、上記の強示唆ワードやそれに類する高設定示唆要素が
    見て取れる場合は、other_info または graph_features に具体的に(何が見えたか)記載してください。
    見当たらない場合は無理に書かず「特になし」としてください。
    """

    prompt = f"""
    パチスロのデータ画面です。以下のJSON形式でのみ出力してください。他の文章は不要です。
    graph_features と other_info は必ず日本語の文章で記述してください(英語や記号だけの出力は不可)。
    {machine_context}
    {{"total_games": 0, "big_count": 0, "reg_count": 0, "current_games": 0, "difference_slabs": 0, "graph_features": "", "other_info": ""}}
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


def analyze_machine_spec_with_gemini(base64_image, mime_type="image/jpeg"):
    """
    機種のスペック表・設定判別要素の画像を解析し、
    機種名・強示唆ワード・ゲームフロー・設定別確率表を抽出する。
    """
    prompt = """
    パチスロ機種のスペック表、または設定判別要素・設定示唆情報が書かれた画像です。
    画像から読み取れる情報をもとに、以下のJSON形式でのみ出力してください。他の文章は不要です。
    値が読み取れない項目は空文字("")や空オブジェクト({})にしてください。数値を推測で埋めないでください。
    machine_name, game_flow, hint_words の内容は必ず日本語で記述してください。

    {
      "machine_name": "画像から読み取れる機種名(正式名称、または特徴的な一部の単語)",
      "hint_words": ["強設定示唆として画像に書かれているキーワードや台詞の一覧"],
      "game_flow": "ゲームフロー・システムの説明(通常時の当選契機、AT/ART中の純増・上乗せ契機、天井など。わかる範囲で簡潔にまとめる)",
      "setting_ratios": {
        "1": {"big": "1/xxx.x", "reg": "1/xxx.x", "total": "1/xxx.x"},
        "2": {"big": "1/xxx.x", "reg": "1/xxx.x", "total": "1/xxx.x"},
        "3": {"big": "1/xxx.x", "reg": "1/xxx.x", "total": "1/xxx.x"},
        "4": {"big": "1/xxx.x", "reg": "1/xxx.x", "total": "1/xxx.x"},
        "5": {"big": "1/xxx.x", "reg": "1/xxx.x", "total": "1/xxx.x"},
        "6": {"big": "1/xxx.x", "reg": "1/xxx.x", "total": "1/xxx.x"}
      }
    }

    setting_ratios は画像に記載されている設定のみを含めてください(全設定が写っていなければ写っている分だけでよい)。
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
        logger.error("Gemini API タイムアウト(機種スペック解析)")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Gemini API 通信エラー(機種スペック解析): {e}")
        return None

    try:
        raw_text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        json_start = raw_text.find("{")
        json_end = raw_text.rfind("}") + 1
        if json_start == -1 or json_end == 0:
            logger.error(f"JSONが見つかりません(機種スペック解析): {raw_text}")
            return None
        return json.loads(raw_text[json_start:json_end])
    except (KeyError, IndexError) as e:
        logger.error(f"Geminiレスポンス構造エラー(機種スペック解析): {e}")
    except json.JSONDecodeError as e:
        logger.error(f"JSON解析エラー(機種スペック解析): {e} / raw={raw_text!r}")
    return None


# ---------------------------------------------------------------------------
# 推測ロジック(AIによる設定判別)
# ---------------------------------------------------------------------------
def find_machine_rule(machine_name):
    """machine_name に一致するキーワードを machines シートから探し、(keyword, rule辞書)を返す"""
    rules = load_machine_rules()
    for keyword, rule in rules.items():
        if keyword and keyword in machine_name:
            return keyword, rule
    return None, {"hint_words": [], "game_flow": "", "setting_ratios": {}}


def _format_setting_ratios(setting_ratios):
    """
    設定別確率表を読みやすいテキストに変換する。
    {"1": {"big": "1/398", ...}, ...} のような構造化データの他、
    スプレッドシートに直接書かれた自由記述テキスト(文字列)にも対応する。
    """
    if not setting_ratios:
        return "登録なし"
    if isinstance(setting_ratios, str):
        return setting_ratios
    lines = []
    for setting_no in sorted(setting_ratios.keys(), key=lambda x: (len(x), x)):
        values = setting_ratios[setting_no]
        if isinstance(values, dict):
            parts = ", ".join(f"{k}:{v}" for k, v in values.items())
        else:
            parts = str(values)
        lines.append(f"設定{setting_no} → {parts}")
    return " / ".join(lines)


def estimate(machine_name, combined_text, stats=None, recent_history_text="", hall_tendency_text=""):
    """
    machine_name・強示唆ワード・ゲームフロー・設定別確率表・累計データ・
    過去のメモやAI備考・同機種の直近の来店データ(とその傾向分析)をGeminiに渡し、
    日本語で設定予測コメントを生成してもらう。
    AI呼び出しに失敗した場合は簡易的なキーワード判定にフォールバックする。
    """
    stats = stats or {}
    matched_keyword, rule = find_machine_rule(machine_name)
    hint_words = rule.get("hint_words", [])
    game_flow = rule.get("game_flow", "")
    setting_ratios = rule.get("setting_ratios", {})

    total_games = stats.get("total_games", 0)
    big_count = stats.get("big_count", 0)
    reg_count = stats.get("reg_count", 0)
    actual_big_rate = f"1/{total_games / big_count:.1f}" if big_count else "算出不可"
    actual_reg_rate = f"1/{total_games / reg_count:.1f}" if reg_count else "算出不可"

    prompt = f"""
    あなたはパチスロの設定判別をサポートするアシスタントです。
    以下の情報をもとに、この台の設定(高設定である可能性)について
    日本語で20〜40文字程度の簡潔な判定コメントを1つだけ出力してください。
    判定コメント以外の説明文・前置き・記号は一切出力しないでください。

    【機種名】{machine_name}
    【この機種の強示唆ワード】{", ".join(hint_words) if hint_words else "登録なし"}
    【この機種のゲームフロー(AT/ART仕様など)】{game_flow if game_flow else "登録なし"}
    【この機種の設定別確率表(スペック表より)】{_format_setting_ratios(setting_ratios)}
    【今回の累計データ】総回転数: {total_games}G, BIG: {big_count}回 (実測確率 {actual_big_rate}), REG: {reg_count}回 (実測確率 {actual_reg_rate}), 現在の回転数: {stats.get("current_games", 0)}G, 差枚: {stats.get("difference_slabs", 0)}枚
    【今回のメモ・AI画像解析結果の蓄積テキスト】{combined_text if combined_text.strip() else "情報なし"}
    【同機種・このホールでの直近(約7日以内)の傾向分析】{hall_tendency_text if hall_tendency_text else "傾向データなし"}
    【同機種の直近の来店データ(個別内訳・参考情報)】{recent_history_text if recent_history_text else "登録なし"}

    設定別確率表が登録されている場合は、実測確率と各設定の理論値を比較して
    最も近い設定帯を推測に反映してください。
    強示唆ワードが蓄積テキストに含まれている場合は高設定寄りのコメントを、
    データが乏しい場合はその旨を踏まえたコメントを出してください。
    「同機種・このホールでの直近の傾向分析」(平均差枚・プラス収支率・強示唆ワード出現頻度など)は、
    そのホールがこの台に対して高設定を使いやすいかどうかの実績を示す重要な材料です。
    設定自体は日ごとにリセットされますが、ホールの設定投入方針(この台をよく使う/据え置きが多いなど)は
    傾向として継続しやすいため、単なる免責事項として退けず、判定に積極的に反映してください。
    傾向が好調(平均差枚がプラス、プラス収支率が高い)であれば強気の判定を、
    傾向が不調であれば慎重な判定を出してください。
    """
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.post(
            GEMINI_URL, headers=headers, data=json.dumps(payload), timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        raw_text = response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        if raw_text:
            return raw_text
    except requests.exceptions.Timeout:
        logger.error("設定予測AI タイムアウト")
    except requests.exceptions.RequestException as e:
        logger.error(f"設定予測AI 通信エラー: {e}")
    except (KeyError, IndexError) as e:
        logger.error(f"設定予測AI レスポンス構造エラー: {e}")

    # AI呼び出しに失敗した場合の簡易フォールバック
    if hint_words and any(hint in combined_text for hint in hint_words):
        return "高設定濃厚!? (要確認/AI判定失敗のため簡易判定)"
    return "推測中...(AI判定失敗)"


# ---------------------------------------------------------------------------
# ルート
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    preset_machine = request.args.get("machine_name", "")
    preset_session = request.args.get("session_id", "")
    history = load_records()
    rules = load_machine_rules()
    machine_list = [
        {
            "keyword": keyword,
            "hint_words": rule.get("hint_words", []),
            "game_flow": rule.get("game_flow", ""),
            "setting_ratios": rule.get("setting_ratios", {}),
        }
        for keyword, rule in rules.items()
    ]

    # 「追加分析」等で機種名が指定されている場合、参考として使われる直近の同機種データをプレビュー表示する
    recent_history_preview = []
    hall_tendency_preview = ""
    if preset_machine:
        _, preview_rule = find_machine_rule(preset_machine)
        recent_history_preview = get_recent_same_machine_records(
            preset_machine, exclude_session_id=preset_session, days=7
        )
        hall_tendency_preview = summarize_hall_tendency(
            recent_history_preview, hint_words=preview_rule.get("hint_words", [])
        )

    return render_template(
        "index.html",
        history=history,
        preset_machine=preset_machine,
        preset_session=preset_session,
        machine_list=machine_list,
        spreadsheet_url=SPREADSHEET_URL,
        machines_sheet_name=MACHINES_SHEET_NAME,
        recent_history_preview=recent_history_preview,
        hall_tendency_preview=hall_tendency_preview,
    )


@app.route("/upload", methods=["POST"])
def upload():
    machine_name = request.form.get("machine_name", "不明な機種").strip()
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
    _, machine_rule = find_machine_rule(machine_name)
    machine_hint_words = machine_rule.get("hint_words", [])
    machine_game_flow = machine_rule.get("game_flow", "")

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
        parsed_data = analyze_image_with_gemini(
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
            graph_features = parsed_data.get("graph_features", "不明")
            other_info = parsed_data.get("other_info", "特になし")
        else:
            flash("画像の解析に失敗しました。手動で確認してください。")
            graph_features, other_info = "解析失敗", "解析失敗"

    # このセッションの過去のメモ・AI備考も合わせて、設定予測をやり直す
    past_text = get_session_history_text(session_id)
    combined_text = " ".join([past_text, user_note, graph_features, other_info])

    # 前日・今週など、同機種の別セッションの記録も参考情報として取得する
    if history_days > 0:
        recent_records = get_recent_same_machine_records(
            machine_name, exclude_session_id=session_id, days=history_days
        )
        recent_history_text = format_recent_history(recent_records)
        hall_tendency_text = summarize_hall_tendency(recent_records, hint_words=machine_hint_words)
    else:
        recent_history_text = "参照しない設定のため未参照"
        hall_tendency_text = "参照しない設定のため未算出"

    stats = {
        "total_games": total,
        "big_count": big,
        "reg_count": reg,
        "current_games": current,
        "difference_slabs": diff,
    }

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
        "estimation": estimate(machine_name, combined_text, stats, recent_history_text, hall_tendency_text),
    }
    save_record(record)
    return redirect(url_for("index"))


@app.route("/machines/upload", methods=["POST"])
def machines_upload():
    manual_keyword = request.form.get("keyword", "").strip()
    manual_hint_words_raw = request.form.get("hint_words", "").strip()
    manual_hint_words = [w.strip() for w in manual_hint_words_raw.split(",") if w.strip()]
    file = request.files.get("spec_image")

    if not file or file.filename == "":
        flash("スペック画像を選択してください。")
        return redirect(url_for("index"))

    if not allowed_file(file.filename):
        flash("対応していないファイル形式です(jpg / jpeg / png / webp のみ)")
        return redirect(url_for("index"))

    ext = file.filename.rsplit(".", 1)[1].lower()
    mime_type = "image/png" if ext == "png" else "image/webp" if ext == "webp" else "image/jpeg"

    image_bytes = file.read()
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    parsed = analyze_machine_spec_with_gemini(base64_image, mime_type)

    if not parsed:
        flash("スペック画像の解析に失敗しました。もう一度お試しください。")
        return redirect(url_for("index"))

    keyword = manual_keyword or str(parsed.get("machine_name", "")).strip()
    if not keyword:
        flash("機種名を読み取れませんでした。機種名キーワードを手入力してください。")
        return redirect(url_for("index"))

    ai_hint_words = parsed.get("hint_words") or []
    if not isinstance(ai_hint_words, list):
        ai_hint_words = []
    # 手入力の強示唆ワードとAI抽出分を合算(重複除去)
    combined_hint_words = list(dict.fromkeys(manual_hint_words + [str(w).strip() for w in ai_hint_words if str(w).strip()]))

    game_flow = str(parsed.get("game_flow", "")).strip()
    setting_ratios = parsed.get("setting_ratios") or {}
    if not isinstance(setting_ratios, dict):
        setting_ratios = {}

    if save_machine_rule(keyword, combined_hint_words, game_flow, setting_ratios):
        flash(f"「{keyword}」の機種データを登録しました。")
    else:
        flash("機種データの保存に失敗しました。")

    return redirect(url_for("index"))


if __name__ == "__main__":
    debug_mode = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)), debug=debug_mode)
