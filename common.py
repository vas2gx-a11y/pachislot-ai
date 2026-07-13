import os
import json
import logging
import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests
import gspread
from google.oauth2.service_account import Credentials
from flask import flash

# --- ロギング設定 ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

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

# --- URLから機種データを取り込む機能の設定 ---
ALLOWED_URL_SCHEMES = {"http", "https"}
URL_FETCH_TIMEOUT = 20  # seconds
URL_FETCH_MAX_BYTES = 3 * 1024 * 1024  # 3MB(取得するHTMLの上限)
URL_TEXT_MAX_CHARS = 18000  # Geminiに渡す本文テキストの最大文字数(長すぎるページは切り詰める)

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

# records シートの列構成(session_idを追加、machine_number/store_nameを追加)
HEADERS = [
    "session_id", "date", "machine_name", "machine_number", "store_name",
    "total_games", "big_count", "reg_count",
    "current_games", "difference_slabs", "graph_features",
    "other_info", "user_note", "estimation", "setting_probabilities",
]

# machines シートの列構成
# keyword: 機種名に含まれるキーワード
# hint_words: 強示唆ワード群(カンマ区切り)
# game_flow: ゲームフロー・システムの説明(AT/ART純増、上乗せ契機など)
# setting_ratios: 設定1〜6ごとの確率(BIG/REG/合成など)をJSON文字列で格納
MACHINE_HEADERS = ["keyword", "hint_words", "game_flow", "setting_ratios"]

# chat_logs シートの列構成(セッションごとのQ&A履歴)
CHAT_SHEET_NAME = os.environ.get("CHAT_SHEET_NAME", "chat_logs")
CHAT_HEADERS = ["session_id", "date", "question", "answer"]

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


def get_chat_worksheet():
    client = get_client()
    sheet = client.open_by_key(SPREADSHEET_ID)
    try:
        ws = sheet.worksheet(CHAT_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=CHAT_SHEET_NAME, rows=1000, cols=len(CHAT_HEADERS))
        ws.append_row(CHAT_HEADERS)
    if ws.row_values(1) != CHAT_HEADERS:
        ws.insert_row(CHAT_HEADERS, 1)
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
            raw_probs = str(r.get("setting_probabilities", "")).strip()
            if raw_probs:
                try:
                    r["setting_probabilities"] = _normalize_setting_probabilities(json.loads(raw_probs))
                except json.JSONDecodeError:
                    r["setting_probabilities"] = {}
            else:
                r["setting_probabilities"] = {}
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
    同じ keyword の行が既にあれば、既存データに新しい内容を追記(マージ)する。
    なければ新規追加する。

    - hint_words: 既存 + 新規 を合算(重複除去)
    - game_flow: 既存の説明文の末尾に新しい説明文を追記(全く同じ内容なら追記しない)
    - setting_ratios: 既存の辞書をベースに、新しいキーで追加・更新(新規に無い既存キーは保持)
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return False

    try:
        ws = get_machines_worksheet()
        existing_keywords = ws.col_values(1)  # 1列目 = keyword
        target_row = None
        for i, value in enumerate(existing_keywords[1:], start=2):  # ヘッダー行を除く
            if str(value).strip() == keyword:
                target_row = i
                break

        # 既存データを読み込む(あれば)
        existing_hint_words = []
        existing_game_flow = ""
        existing_setting_ratios = {}
        if target_row:
            existing_row = ws.row_values(target_row)
            if len(existing_row) > 1:
                existing_hint_words = [w.strip() for w in existing_row[1].split(",") if w.strip()]
            if len(existing_row) > 2:
                existing_game_flow = existing_row[2].strip()
            if len(existing_row) > 3 and existing_row[3].strip():
                try:
                    parsed_existing = json.loads(existing_row[3])
                    if isinstance(parsed_existing, dict):
                        existing_setting_ratios = parsed_existing
                except json.JSONDecodeError:
                    existing_setting_ratios = {}

        # 強示唆ワード: 既存 + 新規をマージ(重複除去、順序維持)
        merged_hint_words = list(dict.fromkeys(
            existing_hint_words + [w.strip() for w in (hint_words or []) if w.strip()]
        ))

        # ゲームフロー: 新しい説明文が既存に含まれていなければ末尾に追記
        new_game_flow = (game_flow or "").strip()
        if new_game_flow and new_game_flow not in existing_game_flow:
            merged_game_flow = (
                f"{existing_game_flow}\n{new_game_flow}".strip("\n")
                if existing_game_flow else new_game_flow
            )
        else:
            merged_game_flow = existing_game_flow

        # 設定判別要素: 既存をベースに新しいキーで追加・更新(保持したまま追記)
        merged_setting_ratios = dict(existing_setting_ratios)
        if isinstance(setting_ratios, dict):
            merged_setting_ratios.update(setting_ratios)

        hint_words_str = ",".join(merged_hint_words)
        setting_ratios_json = json.dumps(merged_setting_ratios, ensure_ascii=False)
        row_values = [keyword, hint_words_str, merged_game_flow, setting_ratios_json]

        if target_row:
            ws.update(f"A{target_row}:D{target_row}", [row_values])
        else:
            ws.append_row(row_values)
        return True
    except Exception as e:
        logger.error(f"機種マスタ書き込みエラー: {e}")
        return False


def load_all_chat_history():
    """
    chat_logs シートの全行を返す(スプレッドシートに追加された順=時系列順)。
    一覧画面で各セッションごとの質問件数・直近の回答をまとめて表示するために使う
    (セッションごとに毎回シートを読みに行くと遅くなるため、1回の読み込みで済ませる)。
    """
    try:
        ws = get_chat_worksheet()
        return ws.get_all_records()
    except Exception as e:
        logger.error(f"チャット全履歴読み込みエラー: {e}")
        return []


def load_chat_history(session_id, limit=20):
    """
    指定セッションのQ&A履歴を古い順(=会話の時系列順)で返す。
    直近のやり取りのみをAIへの文脈として使うため、件数を limit で絞る。
    """
    session_id = str(session_id or "").strip()
    if not session_id:
        return []
    try:
        ws = get_chat_worksheet()
        rows = ws.get_all_records()
        matched = [r for r in rows if str(r.get("session_id", "")) == session_id]
        return matched[-limit:]
    except Exception as e:
        logger.error(f"チャット履歴読み込みエラー: {e}")
        return []


def save_chat_message(session_id, question, answer):
    try:
        ws = get_chat_worksheet()
        ws.append_row([
            str(session_id or ""),
            datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            question,
            answer,
        ])
        return True
    except Exception as e:
        logger.error(f"チャット履歴保存エラー: {e}")
        flash("質問履歴の保存に失敗しました。")
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


def get_recent_same_machine_records(machine_name, store_name="", exclude_session_id="", days=7, limit=5):
    """
    同じ機種名(前日・今週など、別セッションを含む)の直近の記録を取得する。
    store_name が指定されている場合は、同じ店舗(店舗名が完全一致)の記録のみを対象にする。
    (店舗が違えば設定投入方針も変わるため、店舗情報が入力されている場合は店舗を絞り込んで
    ホールの傾向分析の精度を上げる。店舗名が未入力の場合は従来通り店舗を問わず参照する。)
    現在編集中のセッション(exclude_session_id)は除外する。

    「同一店舗・同一台番号・同じ日」の記録は、その日の中で最も新しい1件のみを採用する。
    (同じ台を同じ日に複数セッションで記録した場合の重複カウントを避け、
    日ごと・台ごとの実際の挙動を正しく集計するため。台番号が未入力の記録は
    店舗名+日付のみでまとめて重複排除する簡易対応とする。)
    日付が新しい順に最大limit件返す。
    """
    machine_name = (machine_name or "").strip()
    store_name = (store_name or "").strip()
    if not machine_name:
        return []

    records = load_records()  # 新しい順
    now = datetime.now()

    candidates = []
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
        if store_name:
            r_store = str(r.get("store_name", "")).strip()
            if r_store != store_name:
                continue  # 店舗情報が入力されている場合は、同じ店舗の記録のみ対象にする
        try:
            record_date = datetime.strptime(str(r.get("date", "")), "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        if (now - record_date).days > days:
            continue
        candidates.append((record_date, r))

    # 「同一店舗・同一台番号・同じ日」でグルーピングし、各グループの最新1件のみを残す
    latest_by_group = {}
    for record_date, r in candidates:
        r_store = str(r.get("store_name", "")).strip()
        r_machine_number = str(r.get("machine_number", "")).strip()
        date_only = record_date.strftime("%Y-%m-%d")
        group_key = (r_store, r_machine_number, date_only)
        existing = latest_by_group.get(group_key)
        if existing is None or record_date > existing[0]:
            latest_by_group[group_key] = (record_date, r)

    deduped = sorted(latest_by_group.values(), key=lambda x: x[0], reverse=True)
    return [r for _, r in deduped[:limit]]


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


def summarize_hall_tendency(records, hint_words=None, store_filtered=False):
    """
    直近の同機種データから、そのホール・その台の実際の傾向(平均差枚・勝率・
    強示唆ワードの出現頻度など)を集計し、判定材料として使えるサマリー文を作る。
    store_filtered=True の場合は、店舗名で絞り込んだ「同一店舗」のデータであることを明記する。
    """
    if not records:
        if store_filtered:
            return "傾向データなし(この店舗での過去データが登録されていないため判定不可)"
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

    scope_label = "同一店舗での" if store_filtered else "(店舗情報未入力のため店舗を問わない)"
    summary = (
        f"{scope_label}直近{n}回の平均差枚: {avg_diff:+.0f}枚, "
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
    machine_number(台番号)は、画像内に表示されている台番号・台の管理番号があればその数字や文字列をそのまま読み取ってください。
    見当たらない・読み取れない場合は空文字("")にしてください(推測で埋めないでください)。
    {machine_context}
    {{"total_games": 0, "big_count": 0, "reg_count": 0, "current_games": 0, "difference_slabs": 0, "machine_number": "", "graph_features": "", "other_info": ""}}
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
      "game_flow": "ゲームフロー・システムの説明(通常時の当選契機、AT/ART中の純増・上乗せ契機、天井ゲーム数、狙い目ゾーン(規定G数)など、天井・ゾーン絡みの立ち回り判断に使える情報があれば必ず含めて、わかる範囲で簡潔にまとめる)",
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
# URLから機種スペック情報を取り込む
# ---------------------------------------------------------------------------
class _VisibleTextExtractor(HTMLParser):
    """
    HTMLから <script>/<style> 等を除いた「人間が読める本文テキスト」だけを
    抜き出すための簡易パーサー。ライブラリ追加(BeautifulSoup等)無しで完結させるため、
    標準ライブラリの html.parser のみを使う。
    """

    # 本文として意味の薄いタグの中身は読み飛ばす
    _SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "template"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._chunks = []

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_startendtag(self, tag, attrs):
        pass

    def handle_endtag(self, tag):
        if tag in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data):
        if self._skip_depth == 0:
            text = data.strip()
            if text:
                self._chunks.append(text)

    def get_text(self):
        return "\n".join(self._chunks)


def _is_allowed_url(url):
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in ALLOWED_URL_SCHEMES and bool(parsed.netloc)


def fetch_url_text(url):
    """
    指定されたURLのページを取得し、本文と思われるテキストのみを抽出して返す。
    取得や解析に失敗した場合は None を返す。
    """
    url = (url or "").strip()
    if not url or not _is_allowed_url(url):
        logger.error(f"URL取り込み: 不正なURL: {url!r}")
        return None

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (compatible; PachislotDataBot/1.0; "
            "+machine-spec-import)"
        )
    }

    try:
        response = requests.get(
            url, headers=headers, timeout=URL_FETCH_TIMEOUT, stream=True
        )
        response.raise_for_status()

        content_type = response.headers.get("Content-Type", "")
        if "text/html" not in content_type and "application/xhtml" not in content_type:
            logger.error(f"URL取り込み: HTML以外のコンテンツタイプ: {content_type}")
            return None

        raw_bytes = response.raw.read(URL_FETCH_MAX_BYTES + 1, decode_content=True)
        if len(raw_bytes) > URL_FETCH_MAX_BYTES:
            logger.error("URL取り込み: ページサイズが上限を超えています")
        html_text = raw_bytes.decode(response.encoding or "utf-8", errors="ignore")
    except requests.exceptions.Timeout:
        logger.error("URL取り込み: タイムアウト")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"URL取り込み: 通信エラー: {e}")
        return None

    try:
        parser = _VisibleTextExtractor()
        parser.feed(html_text)
        text = parser.get_text()
    except Exception as e:
        logger.error(f"URL取り込み: HTML解析エラー: {e}")
        return None

    # 連続する空白行を整理しつつ、長すぎる場合は先頭から切り詰める
    text = re.sub(r"\n{2,}", "\n", text).strip()
    if len(text) > URL_TEXT_MAX_CHARS:
        text = text[:URL_TEXT_MAX_CHARS]

    return text or None


def analyze_machine_url_with_gemini(page_text, source_url=""):
    """
    機種解析サイトのページ本文(テキスト)から、機種名・強示唆ワード・ゲームフロー・
    設定別確率表(または設定差データ)を抽出する。analyze_machine_spec_with_gemini() の
    画像版と同じ出力形式(JSON)に揃えることで、そのまま save_machine_rule() に渡せるようにする。
    """
    prompt = f"""
    以下はパチンコ・パチスロの機種解析サイトのページ本文(HTMLからテキストのみ抽出したもの)です。
    ページ内のナビゲーションメニューや広告、口コミなど、機種スペックと関係ない部分は無視してください。
    読み取れる情報をもとに、以下のJSON形式でのみ出力してください。他の文章は一切不要です。
    値が読み取れない項目は空文字("")や空オブジェクト({{}})にしてください。数値やデータを推測で埋めないでください。
    machine_name, game_flow, hint_words の内容は必ず日本語で記述してください。

    {{
      "machine_name": "ページから読み取れる機種名(正式名称、または特徴的な一部の単語)",
      "hint_words": ["強設定示唆として書かれているキーワード・演出名・スタンプ名などの一覧"],
      "game_flow": "ゲームフロー・システムの説明(通常時の当選契機、AT/ART中の純増・上乗せ契機、天井ゲーム数、狙い目ゾーン(規定G数)、機械割など。天井・ゾーン絡みの立ち回り判断に使える情報があれば必ず含めて、わかる範囲で簡潔にまとめる)",
      "setting_ratios": {{
        "1": {{"big": "1/xxx.x", "reg": "1/xxx.x", "total": "1/xxx.x または自由記述の設定差情報"}},
        "2": {{"...": "..."}},
        "3": {{"...": "..."}},
        "4": {{"...": "..."}},
        "5": {{"...": "..."}},
        "6": {{"...": "..."}}
      }}
    }}

    設定ごとのBIG/REG確率表が無い機種(AT/STタイプなど)の場合は、
    setting_ratios の各設定に "total" キーのみで、判明している設定差(例:
    特定演出の出現率、当選率など)を自由記述で構いませんので記載してください。
    情報が全く無い設定は省略して構いません。

    【対象URL】{source_url if source_url else "不明"}
    【ページ本文】
    {page_text}
    """
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.post(
            GEMINI_URL, headers=headers, data=json.dumps(payload), timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
    except requests.exceptions.Timeout:
        logger.error("Gemini API タイムアウト(URL機種データ解析)")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Gemini API 通信エラー(URL機種データ解析): {e}")
        return None

    try:
        raw_text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        json_start = raw_text.find("{")
        json_end = raw_text.rfind("}") + 1
        if json_start == -1 or json_end == 0:
            logger.error(f"JSONが見つかりません(URL機種データ解析): {raw_text}")
            return None
        return json.loads(raw_text[json_start:json_end])
    except (KeyError, IndexError) as e:
        logger.error(f"Geminiレスポンス構造エラー(URL機種データ解析): {e}")
    except json.JSONDecodeError as e:
        logger.error(f"JSON解析エラー(URL機種データ解析): {e} / raw={raw_text!r}")
    return None


# ---------------------------------------------------------------------------
# 推測ロジック(AIによる設定判別)
# ---------------------------------------------------------------------------
def find_machine_rule(machine_name):
    """
    machine_name に一致する登録済み機種を machines シートから探す。

    以前は「辞書の並び順で最初に部分一致したもの」を採用していたため、
    例えば「ToLOVE」と「ToLOVEるダークネス」のように複数のキーワードが
    部分一致する場合に、意図しない(=より一般的で不正確な)機種スペックが
    採用されてしまうことがあった。これを以下の優先順位に修正する:
        1. machine_name とキーワードが完全一致するもの
        2. machine_name に部分一致するキーワードのうち、最も文字数が長い
           (=より具体的な)もの
    """
    machine_name = (machine_name or "").strip()
    if not machine_name:
        return None, {"hint_words": [], "game_flow": "", "setting_ratios": {}}

    rules = load_machine_rules()

    for keyword, rule in rules.items():
        if keyword and keyword.strip() == machine_name:
            return keyword, rule

    candidates = [
        (keyword, rule) for keyword, rule in rules.items()
        if keyword and keyword.strip() and keyword.strip() in machine_name
    ]
    if candidates:
        candidates.sort(key=lambda kv: len(kv[0].strip()), reverse=True)
        return candidates[0]

    logger.warning(f"機種スペック未登録: 「{machine_name}」に一致するキーワードが見つかりませんでした")
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


def _normalize_setting_probabilities(raw):
    """
    {"1": 20, "2": 15, ...} のような設定1〜6の確率(数値)を受け取り、
    6つ全てのキーを持ち、合計がちょうど100になるように整数へ正規化する。
    値が読み取れない/不正な場合は均等配分(情報不足を意味する)にフォールバックする。
    """
    settings = [str(i) for i in range(1, 7)]
    values = {}
    if isinstance(raw, dict):
        for s in settings:
            try:
                v = float(raw.get(s, 0) or 0)
            except (TypeError, ValueError):
                v = 0
            values[s] = max(0.0, v)

    total = sum(values.values()) if values else 0
    if not values or total <= 0:
        # 情報が全く無い場合は完全に均等(=判別材料が無いことを意味する)
        base = 100 // 6
        remainder = 100 - base * 6
        return {s: base + (1 if i < remainder else 0) for i, s in enumerate(settings)}

    # 比率を保ったまま合計100の整数へ丸める(端数は大きい順に配分)
    scaled = {s: values[s] / total * 100 for s in settings}
    floored = {s: int(scaled[s]) for s in settings}
    remainder = 100 - sum(floored.values())
    # 端数が大きい設定から順に+1して合計を100に合わせる
    for s in sorted(settings, key=lambda s: scaled[s] - floored[s], reverse=True)[:remainder]:
        floored[s] += 1
    return floored


def _parse_ratio_string_to_probability(value):
    """
    "1/398.0" や "1/398" のような分数表記、"0.25%" のようなパーセント表記の文字列を
    確率(0〜1の実数)に変換する。変換できない場合は None を返す。
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None

    m = re.match(r"^1\s*/\s*([0-9]+(?:\.[0-9]+)?)$", s)
    if m:
        try:
            denom = float(m.group(1))
            return 1.0 / denom if denom > 0 else None
        except ValueError:
            return None

    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*%$", s)
    if m:
        try:
            return float(m.group(1)) / 100.0
        except ValueError:
            return None

    try:
        v = float(s)
        return v if 0 < v < 1 else None
    except ValueError:
        return None


def _build_setting_match_hint(setting_ratios, total_games, big_count, reg_count):
    """
    機種マスタの設定別確率表(BIG/REGの理論値)と、今回の実測値をPython側で数値比較し、
    実測値に近い順に設定を並べたヒント文を作る。
    分数の比較をAIに丸投げすると計算を誤ることがあるため、事前に計算した結果を
    プロンプトに添えることで判定の精度を上げるのが狙い。
    setting_ratios が自由記述テキストの場合や、必要な数値が無い場合は
    その旨を伝えるメッセージを返す(AIはこの場合テキストの内容から自分で判断する)。
    """
    if not isinstance(setting_ratios, dict) or not setting_ratios:
        return "スペック表(設定別のBIG/REG理論値)が未登録、または自由記述のため自動計算なし"
    if total_games <= 0 or (big_count <= 0 and reg_count <= 0):
        return "累計G数またはBIG/REG回数が不足しているため自動計算なし"

    actual_big_prob = (big_count / total_games) if big_count else None
    actual_reg_prob = (reg_count / total_games) if reg_count else None

    rows = []
    for setting_no in sorted(setting_ratios.keys(), key=lambda x: (len(x), x)):
        values = setting_ratios[setting_no]
        if not isinstance(values, dict):
            continue
        theo_big = _parse_ratio_string_to_probability(values.get("big"))
        theo_reg = _parse_ratio_string_to_probability(values.get("reg"))

        diffs = []
        if actual_big_prob is not None and theo_big:
            diffs.append(abs(actual_big_prob - theo_big) / theo_big)
        if actual_reg_prob is not None and theo_reg:
            diffs.append(abs(actual_reg_prob - theo_reg) / theo_reg)

        if diffs:
            rows.append((setting_no, sum(diffs) / len(diffs)))

    if not rows:
        return "設定別確率表から数値(1/xxx形式)を読み取れないため自動計算なし(自由記述の内容から判断してください)"

    rows.sort(key=lambda x: x[1])
    ranked = " > ".join(f"設定{no}(乖離{diff * 100:.1f}%)" for no, diff in rows)
    return f"実測値に近い順(乖離率が小さいほど実測値に近い): {ranked}"


def _describe_data_volume(total_games, recent_records_count, has_session_history):
    """
    現在の情報量をざっくり3段階(乏しい/普通/十分)で表現し、AIへの指示に使う。
    """
    score = 0
    if total_games >= 1000:
        score += 2
    elif total_games >= 300:
        score += 1
    if recent_records_count >= 3:
        score += 2
    elif recent_records_count >= 1:
        score += 1
    if has_session_history:
        score += 1

    if score <= 1:
        return "乏しい(累計G数が少なく、過去の参考データもほぼ無い)"
    elif score <= 3:
        return "普通(ある程度データはあるが、まだ十分とは言えない)"
    return "十分(累計G数・過去データともに揃っている)"


def estimate(machine_name, combined_text, stats=None, recent_history_text="", hall_tendency_text="",
             recent_records_count=0, base64_image=None, mime_type="image/jpeg"):
    """
    machine_name・強示唆ワード・ゲームフロー・設定別確率表・累計データ・
    過去のメモやAI備考・同機種の直近の来店データ(とその傾向分析)をGeminiに渡し、
    設定1〜6それぞれの確率(%)と、日本語の短い判定コメントを生成してもらう。
    base64_image が渡された場合は、今回アップロードされたデータ画面・グラフ画像も
    そのまま添付し、AIに画像を直接見た上で判定させる(グラフの形状や画面内の
    示唆演出など、テキスト化しきれていない情報を判定材料に加えるため)。
    また、スペック表(設定別のBIG/REG理論値)と実測値の乖離をPython側で事前計算し、
    ヒントとしてプロンプトに含めることで、AIが分数の比較を誤るリスクを減らしている。
    情報が少ない場合は、確率が特定の設定に偏らず均等に近い数値になるよう指示している
    (=数値そのものが「まだ判別材料が少ない」ことを表す)。
    AI呼び出しに失敗した場合は簡易的なキーワード判定にフォールバックする。

    戻り値: (comment: str, setting_probabilities: {"1": int, ..., "6": int})  ※合計100
    """
    stats = stats or {}
    matched_keyword, rule = find_machine_rule(machine_name)
    hint_words = rule.get("hint_words", [])
    game_flow = rule.get("game_flow", "")
    setting_ratios = rule.get("setting_ratios", {})
    if matched_keyword:
        logger.info(f"設定推測: 「{machine_name}」に機種スペック「{matched_keyword}」を適用")
    else:
        logger.info(f"設定推測: 「{machine_name}」に一致する機種スペックが未登録のため、スペック無しで推測")

    total_games = stats.get("total_games", 0)
    big_count = stats.get("big_count", 0)
    reg_count = stats.get("reg_count", 0)
    actual_big_rate = f"1/{total_games / big_count:.1f}" if big_count else "算出不可"
    actual_reg_rate = f"1/{total_games / reg_count:.1f}" if reg_count else "算出不可"
    data_volume = _describe_data_volume(
        total_games, recent_records_count, bool(combined_text and combined_text.strip())
    )
    setting_match_hint = _build_setting_match_hint(setting_ratios, total_games, big_count, reg_count)

    image_instruction = ""
    if base64_image:
        image_instruction = """
    今回アップロードされたデータ画面・グラフの画像も添付しています。テキスト情報だけでなく、
    画像そのものも直接確認し、以下のような視覚的な情報も判定材料に加えてください。
    ・差枚グラフの形状(急増/急落/ジワ増/ジワ減/V字回復/横ばいなど)や、現在の推移の勢い
    ・画面内に表示されている演出・キャラクター・スタンプ・文字色など、強示唆ワードに関連しそうな要素
    ・その他、テキストの数値だけでは伝わらない画面内の情報
    画像から読み取った内容で判定に使ったものがあれば、comment に簡潔に反映してください。
    """

    prompt = f"""
    あなたはパチスロの設定判別をサポートするアシスタントです。
    以下の情報をもとに、この台の設定1〜設定6それぞれである確率(%)を推測し、
    必ず以下のJSON形式のみで出力してください。他の文章・前置き・記号は一切不要です。

    {{"setting_probabilities": {{"1": 0, "2": 0, "3": 0, "4": 0, "5": 0, "6": 0}}, "comment": "20〜40文字程度の日本語コメント"}}

    【機種名】{machine_name}
    【この機種の強示唆ワード】{", ".join(hint_words) if hint_words else "登録なし"}
    【この機種のゲームフロー(AT/ART仕様など)】{game_flow if game_flow else "登録なし"}
    【この機種の設定別確率表(スペック表より)】{_format_setting_ratios(setting_ratios)}
    【実測値と設定別理論値の自動比較(Pythonで計算済み、参考値として重視してください)】{setting_match_hint}
    【今回の累計データ】総回転数: {total_games}G, BIG: {big_count}回 (実測確率 {actual_big_rate}), REG: {reg_count}回 (実測確率 {actual_reg_rate}), 現在の回転数: {stats.get("current_games", 0)}G, 差枚: {stats.get("difference_slabs", 0)}枚
    【今回のメモ・AI画像解析結果の蓄積テキスト】{combined_text if combined_text.strip() else "情報なし"}
    【同機種・このホールでの直近(約7日以内)の傾向分析】{hall_tendency_text if hall_tendency_text else "傾向データなし"}
    【同機種の直近の来店データ(個別内訳・参考情報)】{recent_history_text if recent_history_text else "登録なし"}
    【現時点の情報量】{data_volume}
    {image_instruction}
    setting_probabilities の6つの値は、合計がちょうど100になるように整数で出力してください。
    情報量が「乏しい」場合は、特定の設定に偏らせず16〜17%前後の均等に近い数値にしてください
    (=まだ判別材料が少ないことを数値そのもので表現してください)。
    情報量が「普通」「十分」で、実測確率のズレ・強示唆ワード・ホールの傾向などから
    高設定/低設定の可能性が読み取れる場合は、該当する設定に大きく偏らせて構いません。
    設定別確率表が登録されている場合は、実測確率と各設定の理論値を比較して
    最も近い設定帯の確率を高めに評価してください。特に「実測値と設定別理論値の自動比較」の
    結果は事前に計算済みの正確な数値なので、優先して参考にしてください。
    「同機種・このホールでの直近の傾向分析」(平均差枚・プラス収支率・強示唆ワード出現頻度など)は、
    そのホールがこの台に対して高設定を使いやすいかどうかの実績を示す重要な材料なので、
    単なる免責事項として退けず、確率分布に積極的に反映してください。
    comment には判定の根拠となった主なポイントを簡潔に含めてください。
    """
    parts = [{"text": prompt}]
    if base64_image:
        parts.append({"inlineData": {"mimeType": mime_type, "data": base64_image}})
    payload = {"contents": [{"parts": parts}]}
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.post(
            GEMINI_URL, headers=headers, data=json.dumps(payload), timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        raw_text = response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        json_start = raw_text.find("{")
        json_end = raw_text.rfind("}") + 1
        if json_start != -1 and json_end != 0:
            parsed = json.loads(raw_text[json_start:json_end])
            comment = str(parsed.get("comment", "")).strip() or "判定コメントなし"
            probabilities = _normalize_setting_probabilities(parsed.get("setting_probabilities"))
            return comment, probabilities
        logger.error(f"設定予測AI JSONが見つかりません: {raw_text}")
    except requests.exceptions.Timeout:
        logger.error("設定予測AI タイムアウト")
    except requests.exceptions.RequestException as e:
        logger.error(f"設定予測AI 通信エラー: {e}")
    except (KeyError, IndexError) as e:
        logger.error(f"設定予測AI レスポンス構造エラー: {e}")
    except json.JSONDecodeError as e:
        logger.error(f"設定予測AI JSON解析エラー: {e}")

    # AI呼び出しに失敗した場合の簡易フォールバック
    if hint_words and any(hint in combined_text for hint in hint_words):
        fallback_probabilities = _normalize_setting_probabilities(
            {"1": 5, "2": 5, "3": 10, "4": 20, "5": 30, "6": 30}
        )
        return "高設定濃厚!? (要確認/AI判定失敗のため簡易判定)", fallback_probabilities
    return "推測中...(AI判定失敗)", _normalize_setting_probabilities({})


# ---------------------------------------------------------------------------
# 分析結果へのQ&A(セッション単位のチャット)
# ---------------------------------------------------------------------------
def _format_session_records_for_chat(records):
    """
    セッション内の記録(古い順)を、チャット用プロンプトで読みやすい時系列テキストに変換する。
    """
    if not records:
        return "登録なし"
    ordered = sorted(records, key=lambda r: str(r.get("date", "")))
    lines = []
    for r in ordered:
        probs = r.get("setting_probabilities") or {}
        if isinstance(probs, dict) and probs:
            probs_text = ", ".join(f"設定{s}:{probs.get(s, 0)}%" for s in sorted(probs, key=lambda x: (len(x), x)))
        else:
            probs_text = "算出なし"
        lines.append(
            f"[{r.get('date', '')}] 総回転数{r.get('total_games', 0)}G, "
            f"現在の回転数(前回BIG/REGからのG数、天井・ゾーン判断用){r.get('current_games', 0)}G, "
            f"BIG{r.get('big_count', 0)}回, REG{r.get('reg_count', 0)}回, "
            f"差枚{r.get('difference_slabs', 0)}枚, メモ:{r.get('user_note', '') or 'なし'}, "
            f"AI備考:{r.get('other_info', '') or 'なし'}, "
            f"その時点の推測:{r.get('estimation', '') or 'なし'}({probs_text})"
        )
    return "\n".join(lines)


def answer_question(session_id, machine_name, question, chat_history=None):
    """
    特定セッションの蓄積データ・機種スペック・直近の設定推測結果・ホールの傾向分析・
    これまでのQ&A履歴をもとに、ユーザーからの自由な質問(例:「このまま打ち続けるべき?」)
    にAIが日本語で回答する。

    chat_history には [(質問, 回答), ...] の形式でこれまでのやり取りを渡すと、
    その文脈を踏まえた回答になる(例: 「さっきの続きだけど〜」のような質問にも対応しやすくなる)。

    戻り値: 回答テキスト(str)。失敗時はエラーを説明する日本語メッセージを返す。
    """
    session_id = str(session_id or "").strip()
    question = (question or "").strip()
    if not question:
        return "質問内容が空でした。"

    all_records = load_records()
    session_records = [r for r in all_records if str(r.get("session_id", "")) == session_id]
    if not session_records:
        return "このセッションのデータが見つかりませんでした。まずデータを1件以上登録してください。"

    latest = session_records[0]  # load_records() は新しい順
    machine_name = machine_name or latest.get("machine_name", "")
    store_name = str(latest.get("store_name", "")).strip()

    matched_keyword, rule = find_machine_rule(machine_name)
    hint_words = rule.get("hint_words", [])
    game_flow = rule.get("game_flow", "")
    setting_ratios = rule.get("setting_ratios", {})

    session_records_text = _format_session_records_for_chat(session_records)
    latest_probs = latest.get("setting_probabilities") or {}
    if isinstance(latest_probs, dict) and latest_probs:
        latest_probs_text = ", ".join(
            f"設定{s}:{latest_probs.get(s, 0)}%" for s in sorted(latest_probs, key=lambda x: (len(x), x))
        )
    else:
        latest_probs_text = "算出なし"

    recent_records = get_recent_same_machine_records(
        machine_name, store_name=store_name, exclude_session_id=session_id, days=7
    )
    hall_tendency_text = summarize_hall_tendency(
        recent_records, hint_words=hint_words, store_filtered=bool(store_name)
    )

    chat_history = chat_history or []
    if chat_history:
        chat_history_text = "\n".join(f"Q: {q}\nA: {a}" for q, a in chat_history)
    else:
        chat_history_text = "なし(このセッションでの初めての質問)"

    latest_current_games = latest.get("current_games", 0)

    prompt = f"""
    あなたはパチスロの実戦データ分析をサポートするアシスタントです。
    ユーザーは実際にこの台を打っており、これまで記録してきたデータをもとに質問しています。
    質問には「天井まで/ゾーンまであと何G様子見すべきか」のような、ゲーム数を絡めた
    立ち回りの相談も含まれます。【この機種のゲームフロー】に天井ゲーム数やゾーン(規定G数)の
    情報が含まれている場合は、【現在の回転数】と照らし合わせて、
    具体的な残りゲーム数の目安や、続行/様子見/ヤメの判断を必ず含めて回答してください。
    以下の情報を踏まえて、質問に日本語で具体的に回答してください(150〜250文字程度を目安に、
    箇条書きが適切な場合は箇条書きを使っても構いません)。
    断定的な保証(必ず勝てる等)はできないため、「データから読み取れる傾向としては」という
    立場から、根拠を示しつつ答えてください。実際にやめるかどうかの最終判断はユーザー自身に
    委ねる姿勢を保ちつつ、データに基づいた具体的な意見は述べてください(単なる一般論や
    「自己責任で」で終わらせないこと)。

    【機種名】{machine_name}
    【この機種の強示唆ワード】{", ".join(hint_words) if hint_words else "登録なし"}
    【この機種のゲームフロー(AT/ART仕様、天井ゲーム数、ゾーンなど)】{game_flow if game_flow else "登録なし"}
    【この機種の設定別確率表(スペック表より)】{_format_setting_ratios(setting_ratios)}
    【現在の回転数(前回BIG/REGからのG数、天井・ゾーン判断の基準になる数値)】{latest_current_games}G
    【このセッションの蓄積データ(時系列、記録するたびに再解析している)】
    {session_records_text}
    【直近(最新)の設定確率推測結果】{latest_probs_text} (コメント: {latest.get('estimation', '') or 'なし'})
    【同機種・このホールでの直近(約7日以内、このセッションを除く)の傾向分析】{hall_tendency_text if hall_tendency_text else "傾向データなし"}
    【このセッションでのこれまでのQ&A】
    {chat_history_text}
    【今回の質問】{question}

    回答のみを出力してください(前置きや「回答:」等のラベル、Markdown記法は不要です)。
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
        logger.error("Q&A AI: 空の応答")
    except requests.exceptions.Timeout:
        logger.error("Q&A AI タイムアウト")
    except requests.exceptions.RequestException as e:
        logger.error(f"Q&A AI 通信エラー: {e}")
    except (KeyError, IndexError) as e:
        logger.error(f"Q&A AI レスポンス構造エラー: {e}")

    return "回答の生成に失敗しました。もう一度お試しください。"


# ---------------------------------------------------------------------------
# 期待値計算(天井/ゾーン狙いの単純期待値)
# ---------------------------------------------------------------------------
def calculate_expected_value(current_games, target_games, coin_cost_per_game, expected_payout, exchange_rate):
    """
    「現在のG数から、天井やゾーンなど目標G数まで様子見して打つ」ケースの
    単純な期待値計算(決定論的な計算のみで行う。お金に関わる計算のためAIは使わない)。

    現在G数から目標G数までの残りG数を投資コストとして見積もり、
    目標到達時に見込める期待獲得枚数(円換算)と比較して期待値を算出する。
    設定差や実際の当選確率のブレは考慮しない、あくまで単純な損益分岐の目安。

    current_games: 現在の回転数(G)
    target_games: 狙う目標G数(天井やゾーンのG数)
    coin_cost_per_game: 1Gあたりの投資額(円)。20円スロットなら20円/Gが目安。
    expected_payout: 目標到達時に見込める期待獲得枚数(枚)
    exchange_rate: 交換レート(円/枚)

    戻り値: 計算結果の辞書(remaining_games, investment_yen, expected_payout,
             expected_return_yen, expected_value_yen, expected_value_per_game, is_plus)
    """
    remaining_games = max(target_games - current_games, 0)
    investment_yen = remaining_games * coin_cost_per_game
    expected_return_yen = expected_payout * exchange_rate
    expected_value_yen = expected_return_yen - investment_yen
    expected_value_per_game = (expected_value_yen / remaining_games) if remaining_games > 0 else 0.0

    return {
        "remaining_games": remaining_games,
        "investment_yen": investment_yen,
        "expected_payout": expected_payout,
        "expected_return_yen": expected_return_yen,
        "expected_value_yen": expected_value_yen,
        "expected_value_per_game": expected_value_per_game,
        "is_plus": expected_value_yen > 0,
    }


def estimate_expected_payout_with_gemini(machine_name, target_games, current_games):
    """
    機種スペック(ゲームフロー)をもとに、目標ゲーム数(天井/ゾーンなど)に到達した際に
    見込める「平均的な期待獲得枚数」の目安をAIに概算してもらう。

    注意: これは公表されている統計値ではなく、登録されているゲームフローのテキストから
    AIが読み取れる範囲で推測した参考値に過ぎない。情報が不十分な場合は概算しない。

    戻り値: (expected_payout: float | None, note: str)
    """
    machine_name = (machine_name or "").strip()
    matched_keyword, rule = find_machine_rule(machine_name)
    game_flow = rule.get("game_flow", "")
    if not game_flow:
        return None, "この機種のゲームフロー情報が未登録のため、AIによる期待獲得枚数の概算はできません。手動で入力してください。"

    prompt = f"""
    以下はパチスロ機種のゲームフロー情報です。この情報だけから、
    目標ゲーム数({target_games}G、天井やゾーンなど)に到達した際に見込める
    「平均的な期待獲得枚数(差枚)」のごくおおまかな目安を推測してください。
    正確な統計値ではなく、ゲームフロー情報の記述(AT初期ゲーム数、上乗せ傾向、
    ハーレムモード等の上位状態への移行率など)から読み取れる範囲での目安で構いません。
    情報が不十分で妥当な推測ができない場合は、無理に数値を出さず 0 を返してください。

    以下のJSON形式のみで出力してください。他の文章は一切不要です。
    {{"expected_payout": 0, "note": "推測の前提や根拠を50文字程度で(日本語)"}}

    【機種名】{machine_name}
    【ゲームフロー】{game_flow}
    【現在の回転数】{current_games}G
    【目標ゲーム数】{target_games}G
    """
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.post(
            GEMINI_URL, headers=headers, data=json.dumps(payload), timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        raw_text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        json_start = raw_text.find("{")
        json_end = raw_text.rfind("}") + 1
        if json_start == -1 or json_end == 0:
            return None, "AIによる概算に失敗しました。手動で入力してください。"
        parsed = json.loads(raw_text[json_start:json_end])
        payout = float(parsed.get("expected_payout", 0) or 0)
        note = str(parsed.get("note", "")).strip()
        if payout <= 0:
            return None, note or "AIによる期待獲得枚数の概算ができませんでした。手動で入力してください。"
        return payout, (note + "(AIによる概算値・参考程度に)" if note else "AIによる概算値(参考程度に)")
    except requests.exceptions.Timeout:
        logger.error("期待獲得枚数AI概算: タイムアウト")
    except requests.exceptions.RequestException as e:
        logger.error(f"期待獲得枚数AI概算: 通信エラー: {e}")
    except (KeyError, IndexError, json.JSONDecodeError, TypeError, ValueError) as e:
        logger.error(f"期待獲得枚数AI概算: 解析エラー: {e}")

    return None, "AIによる概算に失敗しました。手動で入力してください。"
