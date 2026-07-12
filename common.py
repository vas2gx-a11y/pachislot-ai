import os
import json
import logging
import re
import html
from datetime import datetime

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
SPEC_IMPORT_TIMEOUT = 60  # URLからのスペック一括取り込みはページが長文になるため長めに確保
PAGE_TEXT_MAX_CHARS = 120000  # ページ本文からGeminiに渡すテキストの上限文字数

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "pdf"}
MAX_UPLOAD_SIZE = 8 * 1024 * 1024  # 8MB

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
# basic_info: 型式名・メーカー名・機械割・導入開始日・機種概要など(JSON文字列)
# game_flow: ゲームフロー・システムの説明(AT/ART純増、上乗せ契機など)
# setting_ratios: 設定1〜6ごとの確率(BIG/REG/合成など)をJSON文字列で格納
# ceiling_info: 天井ゲーム数・突入条件・恩恵・リセット仕様・ヤメ時など(JSON文字列)
# bonus_info: ボーナス当選率・平均獲得枚数・1Gあたりの純増など(JSON文字列)
# cz_info: CZの種類・当選確率・期待度など(JSON文字列)
# suggestion_info: 終了画面示唆・獲得枚数示唆・内部状態示唆などの設定示唆演出(JSON文字列)
# extra_info: 上記に当てはまらないが重要な情報(発明品・演出詳細など、自由記述)
# source_url: この機種データを取り込んだ元ページのURL(参考用)
MACHINE_HEADERS = [
    "keyword", "hint_words", "basic_info", "game_flow", "setting_ratios",
    "ceiling_info", "bonus_info", "cz_info", "suggestion_info", "extra_info", "source_url",
]

# 上記のうち、既存データに対して辞書としてマージ(update)する項目
_JSON_MERGE_FIELDS = {"basic_info", "setting_ratios", "ceiling_info", "bonus_info", "cz_info", "suggestion_info"}
# 上記のうち、既存データに対してテキストとして追記マージする項目
_TEXT_MERGE_FIELDS = {"game_flow", "extra_info"}

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
            ws.append_row([rule.get(h, "") for h in MACHINE_HEADERS])
    if ws.row_values(1) != MACHINE_HEADERS:
        # 列構成が変わった場合はヘッダー行を挿入する。
        # 既存のmachinesシートが旧スキーマ(4列)のままだと、この操作で新ヘッダー行が
        # 先頭に追加されるだけで既存データの列は自動移行されないため、
        # 列構成を変更した際は一度シートの中身を確認してください。
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


def get_record_by_session(session_id):
    """
    指定した session_id の記録を records シートから1件取得する(無ければ None)。
    継続セッション(追加分析)の際、前回までの内容を引き継ぐために使う。
    """
    session_id = str(session_id or "").strip()
    if not session_id:
        return None
    try:
        ws = get_records_worksheet()
        session_ids = ws.col_values(1)  # 1列目 = session_id
        for i, value in enumerate(session_ids[1:], start=2):  # ヘッダー行を除く
            if str(value).strip() == session_id:
                row_values = ws.row_values(i)
                record = {h: (row_values[idx] if idx < len(row_values) else "") for idx, h in enumerate(HEADERS)}
                for field in NUMERIC_FIELDS:
                    record[field] = _to_int(record.get(field, 0))
                return record
        return None
    except Exception as e:
        logger.error(f"セッション記録読み込みエラー: {e}")
        return None


def save_or_update_record(record):
    """
    records シートに記録を保存する。
    同じ session_id の行が既にあれば、その行を丸ごと上書き更新する(1セッション = 1行)。
    無ければ新規に行を追加する。
    (追加分析のたびに行が増えて履歴が分裂しないようにするための仕組み。
     項目のマージ・引き継ぎ・結合は呼び出し側で行ってから渡すこと。)
    """
    session_id = str(record.get("session_id", "")).strip()
    try:
        ws = get_records_worksheet()
        target_row = None
        if session_id:
            session_ids = ws.col_values(1)
            for i, value in enumerate(session_ids[1:], start=2):
                if str(value).strip() == session_id:
                    target_row = i
                    break

        row_values = [record.get(h, "") for h in HEADERS]
        if target_row:
            last_col = chr(ord("A") + len(HEADERS) - 1)  # HEADERSは15列 → "O"
            ws.update(f"A{target_row}:{last_col}{target_row}", [row_values])
        else:
            ws.append_row(row_values)
    except Exception as e:
        logger.error(f"スプレッドシート書き込みエラー: {e}")
        flash("スプレッドシートへの保存に失敗しました。")


# 新しい内容が無い/意味の無いプレースホルダーの場合は結合対象から除外する
_MERGE_PLACEHOLDER_VALUES = {"", "画像なし", "特になし", "解析失敗", "不明"}


def merge_text_field(old_text, new_text):
    """
    既存のテキスト(old_text)に新しいテキスト(new_text)を追記して結合する。
    - new_text が空/プレースホルダー("画像なし"等)なら何もしない
    - new_text が old_text に既に含まれていれば重複追記しない
    - それ以外は old_text の末尾に改行区切りで追記する
    """
    old_text = (old_text or "").strip()
    new_text = (new_text or "").strip()
    if new_text in _MERGE_PLACEHOLDER_VALUES:
        return old_text
    if not old_text:
        return new_text
    if new_text in old_text:
        return old_text
    return f"{old_text}\n{new_text}"


def _parse_json_cell(raw):
    """スプレッドシートのセル文字列をJSONとしてパースする。dict以外/失敗時は{}を返す"""
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _load_structured_field(raw):
    """
    machines シートのJSON項目セルを読み込む。
    JSONとして解釈できればその辞書を、できなければ自由記述テキストとして
    {"raw": "..."} の形にして返す(スプレッドシートに直接自由文を書き込んでいた場合に対応)。
    """
    raw = str(raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"raw": str(parsed)}
    except json.JSONDecodeError:
        return {"raw": raw}


def load_machine_rules():
    """
    machines シートから
    {keyword: {"hint_words": [...], "game_flow": "...", "setting_ratios": {...},
               "basic_info": {...}, "ceiling_info": {...}, "bonus_info": {...},
               "cz_info": {...}, "suggestion_info": {...}, "extra_info": "...",
               "source_url": "..."}}
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
            rules[keyword] = {
                "hint_words": hint_words,
                "game_flow": str(row.get("game_flow", "")).strip(),
                "extra_info": str(row.get("extra_info", "")).strip(),
                "source_url": str(row.get("source_url", "")).strip(),
                "basic_info": _load_structured_field(row.get("basic_info", "")),
                "setting_ratios": _load_structured_field(row.get("setting_ratios", "")),
                "ceiling_info": _load_structured_field(row.get("ceiling_info", "")),
                "bonus_info": _load_structured_field(row.get("bonus_info", "")),
                "cz_info": _load_structured_field(row.get("cz_info", "")),
                "suggestion_info": _load_structured_field(row.get("suggestion_info", "")),
            }
        return rules
    except Exception as e:
        logger.error(f"機種マスタ読み込みエラー: {e}")
        return {}


def save_machine_rule(keyword, fields):
    """
    machines シートに機種情報を保存する。
    同じ keyword の行が既にあれば、既存データに新しい内容を項目ごとにマージする。
    なければ新規追加する。

    fields には MACHINE_HEADERS の keyword 以外の項目のうち、更新したいものだけ渡せばよい。
      - hint_words: list[str]                          → 既存+新規をマージ(重複除去)
      - game_flow / extra_info: str                     → 既存の末尾に追記(重複・空は無視)
      - basic_info / setting_ratios / ceiling_info /
        bonus_info / cz_info / suggestion_info: dict     → 既存の辞書に対してupdate(保持したまま追記・上書き)
      - source_url: str                                  → 最新のものに置き換え
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return False
    fields = fields or {}

    try:
        ws = get_machines_worksheet()
        existing_keywords = ws.col_values(1)  # 1列目 = keyword
        target_row = None
        for i, value in enumerate(existing_keywords[1:], start=2):  # ヘッダー行を除く
            if str(value).strip() == keyword:
                target_row = i
                break

        # 既存データを読み込む(あれば)
        existing = {h: "" for h in MACHINE_HEADERS}
        if target_row:
            existing_row = ws.row_values(target_row)
            for idx, h in enumerate(MACHINE_HEADERS):
                if idx < len(existing_row):
                    existing[h] = existing_row[idx]

        merged = {"keyword": keyword}

        # 強示唆ワード: 既存 + 新規をマージ(重複除去、順序維持)
        existing_hint_words = [w.strip() for w in existing.get("hint_words", "").split(",") if w.strip()]
        new_hint_words = [str(w).strip() for w in (fields.get("hint_words") or []) if str(w).strip()]
        merged["hint_words"] = ",".join(list(dict.fromkeys(existing_hint_words + new_hint_words)))

        # テキスト追記型(ゲームフロー・その他情報): 新しい内容が既存に無ければ末尾に追記
        for field in _TEXT_MERGE_FIELDS:
            merged[field] = merge_text_field(existing.get(field, ""), fields.get(field, ""))

        # JSON(dict)マージ型: 既存の辞書をベースに新しいキーで追加・更新(保持したまま追記)
        for field in _JSON_MERGE_FIELDS:
            existing_dict = _parse_json_cell(existing.get(field, ""))
            new_value = fields.get(field)
            if isinstance(new_value, dict):
                existing_dict.update(new_value)
            merged[field] = json.dumps(existing_dict, ensure_ascii=False)

        # 取り込み元URL: 新しいものがあれば置き換え、無ければ既存を維持
        merged["source_url"] = str(fields.get("source_url") or existing.get("source_url", "")).strip()

        row_values = [merged.get(h, "") for h in MACHINE_HEADERS]

        if target_row:
            last_col = chr(ord("A") + len(MACHINE_HEADERS) - 1)  # MACHINE_HEADERSは11列 → "K"
            ws.update(f"A{target_row}:{last_col}{target_row}", [row_values])
        else:
            ws.append_row(row_values)
        return True
    except Exception as e:
        logger.error(f"機種マスタ書き込みエラー: {e}")
        return False


def fetch_page_text(url, max_chars=PAGE_TEXT_MAX_CHARS):
    """
    指定したURLのページを取得し、HTMLタグ等を除去したテキストを返す。
    (機種スペック情報サイトなどからスペック情報を一括で取り込むために使う)
    取得や解析に失敗した場合は None を返す。
    """
    try:
        resp = requests.get(
            url, timeout=REQUEST_TIMEOUT, headers={"User-Agent": "Mozilla/5.0 (compatible; SpecImportBot/1.0)"}
        )
        resp.raise_for_status()
    except requests.exceptions.Timeout:
        logger.error(f"ページ取得タイムアウト: {url}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"ページ取得エラー: {url} / {e}")
        return None

    html_text = resp.text
    # script/styleタグは中身ごと除去
    html_text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html_text, flags=re.DOTALL | re.IGNORECASE)
    # 残りのHTMLタグを除去
    text = re.sub(r"<[^>]+>", " ", html_text)
    # HTMLエンティティ(&amp;等)をデコード
    text = html.unescape(text)
    # 余分な空白・空行を圧縮
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    text = text.strip()

    if len(text) > max_chars:
        text = text[:max_chars]
    return text

def get_recent_same_machine_records(machine_name, store_name="", exclude_session_id="", days=7, limit=5):
    """
    同じ機種名(前日・今週など、別セッションを含む)の直近の記録を取得する。
    store_name が指定されている場合は、同じ店舗(店舗名が完全一致)の記録のみを対象にする。
    (店舗が違えば設定投入方針も変わるため、店舗情報が入力されている場合は店舗を絞り込んで
    ホールの傾向分析の精度を上げる。店舗名が未入力の場合は従来通り店舗を問わず参照する。)
    現在編集中のセッション(exclude_session_id)は除外し、日付が新しい順に最大limit件返す。
    ※ セッション単位で最新1件のみを採用する(同じ来店で何度も記録した分の重複を避けるため)。
    """
    machine_name = (machine_name or "").strip()
    store_name = (store_name or "").strip()
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
    このファイルは「{machine_name}」のデータ画面(または関連する画面・資料)です。
    この機種で登録されている強示唆ワード: {", ".join(hint_words) if hint_words else "登録なし"}
    この機種のゲームフロー: {game_flow if game_flow else "登録なし"}
    ファイル内の文字・演出・グラフに、上記の強示唆ワードやそれに類する高設定示唆要素が
    見て取れる場合は、other_info または graph_features に具体的に(何が見えたか)記載してください。
    見当たらない場合は無理に書かず「特になし」としてください。
    """

    prompt = f"""
    パチスロのデータ画面です(画像またはPDFで渡されます。PDFの場合は複数ページあれば全ページ分の内容を踏まえてください)。
    以下のJSON形式でのみ出力してください。他の文章は不要です。
    graph_features と other_info は必ず日本語の文章で記述してください(英語や記号だけの出力は不可)。
    machine_number(台番号)は、ファイル内に表示されている台番号・台の管理番号があればその数字や文字列をそのまま読み取ってください。
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
    機種名・強示唆ワード・ゲームフロー・設定別確率表・天井・ボーナス・CZ・設定示唆演出などを抽出する。
    """
    prompt = """
    パチスロ機種のスペック表、または設定判別要素・設定示唆情報が書かれた画像です。
    画像から読み取れる情報をもとに、以下のJSON形式でのみ出力してください。他の文章は不要です。
    値が読み取れない項目は空文字("")や空オブジェクト({})にしてください。数値を推測で埋めないでください。
    machine_name, hint_words, game_flow, extra_info の内容は必ず日本語で記述してください。

    {
      "machine_name": "画像から読み取れる機種名(正式名称、または特徴的な一部の単語)",
      "hint_words": ["強設定示唆として画像に書かれているキーワードや台詞の一覧"],
      "game_flow": "ゲームフロー・システムの説明(通常時の当選契機、AT/ART中の純増・上乗せ契機など。わかる範囲で簡潔にまとめる)",
      "basic_info": {"型式名": "", "メーカー名": "", "機械割": "", "導入開始日": "", "機種概要": ""},
      "setting_ratios": {
        "1": {"big": "1/xxx.x", "reg": "1/xxx.x", "total": "1/xxx.x"},
        "2": {}, "3": {}, "4": {}, "5": {}, "6": {}
      },
      "ceiling_info": {"天井ゲーム数": "", "設定変更後の天井": "", "天井突入条件": "", "天井恩恵": "", "リセット仕様": "", "ヤメ時": ""},
      "bonus_info": {"ボーナス当選率": "", "平均獲得枚数": "", "1Gあたりの純増": ""},
      "cz_info": {"CZ種類と確率": "", "CZ期待度": ""},
      "suggestion_info": {"終了画面示唆": "", "獲得枚数示唆": "", "内部状態示唆": "", "その他の設定示唆演出": ""},
      "extra_info": "上記項目に当てはまらないが重要そうな情報を簡潔にまとめたもの"
    }

    setting_ratios は画像に記載されている設定のみを含めてください(全設定が写っていなければ写っている分だけでよい)。
    basic_info / ceiling_info / bonus_info / cz_info / suggestion_info は、
    画像から読み取れた項目だけキーを含めてください(無理に全キーを埋めなくてよい)。
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


def analyze_machine_spec_from_text(page_text, source_url=""):
    """
    機種スペック・解析情報サイトなどのページ本文(HTMLタグ除去済みテキスト)から、
    機種名・強示唆ワード・ゲームフロー・設定別確率・天井・ボーナス・CZ・設定示唆演出などを
    まとめて抽出する。ナビゲーションメニューや広告・関連記事・口コミなど無関係な文章が
    混ざっていても、機種スペックに関係する部分だけを読み取るようGeminiに指示する。
    """
    prompt = f"""
    以下はパチスロ機種のスペック・解析情報サイトのページ本文です。
    ナビゲーションメニューや広告・関連記事・ユーザー口コミなど、機種スペックと関係ない文章も
    混ざっていることがあるので、機種スペックに関係する部分だけを読み取ってください。
    読み取れた情報をもとに、以下のJSON形式でのみ出力してください。他の文章は不要です。
    値が読み取れない項目は空文字("")や空オブジェクト({{}})にしてください。数値を推測で埋めないでください。
    machine_name, hint_words, game_flow, extra_info の内容は必ず日本語で記述してください。

    {{
      "machine_name": "機種名(正式名称、または特徴的な一部の単語)",
      "hint_words": ["強設定示唆として書かれているキーワードや台詞の一覧"],
      "game_flow": "ゲームフロー・システムの説明(通常時の当選契機、AT/ART中の純増・上乗せ契機など。簡潔にまとめる)",
      "basic_info": {{"型式名": "", "メーカー名": "", "機械割": "", "導入開始日": "", "機種概要": ""}},
      "setting_ratios": {{
        "1": {{"big": "1/xxx.x", "reg": "1/xxx.x", "total": "1/xxx.x"}},
        "2": {{}}, "3": {{}}, "4": {{}}, "5": {{}}, "6": {{}}
      }},
      "ceiling_info": {{"天井ゲーム数": "", "設定変更後の天井": "", "天井突入条件": "", "天井恩恵": "", "リセット仕様": "", "ヤメ時": ""}},
      "bonus_info": {{"ボーナス当選率": "", "平均獲得枚数": "", "1Gあたりの純増": ""}},
      "cz_info": {{"CZ種類と確率": "", "CZ期待度": ""}},
      "suggestion_info": {{"終了画面示唆": "", "獲得枚数示唆": "", "内部状態示唆": "", "その他の設定示唆演出": ""}},
      "extra_info": "上記項目に当てはまらないが重要そうな情報(発明品・演出詳細など)を簡潔にまとめたもの"
    }}

    setting_ratios は全設定が書かれていなくても、書かれている設定だけで構いません。
    basic_info / ceiling_info / bonus_info / cz_info / suggestion_info は、
    読み取れた項目だけキーを含めてください(無理に全キーを埋めなくてよい)。

    --- ページ本文 ---
    {page_text}
    """
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.post(
            GEMINI_URL, headers=headers, data=json.dumps(payload), timeout=SPEC_IMPORT_TIMEOUT
        )
        response.raise_for_status()
    except requests.exceptions.Timeout:
        logger.error("Gemini API タイムアウト(URL機種スペック解析)")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Gemini API 通信エラー(URL機種スペック解析): {e}")
        return None

    try:
        raw_text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        json_start = raw_text.find("{")
        json_end = raw_text.rfind("}") + 1
        if json_start == -1 or json_end == 0:
            logger.error(f"JSONが見つかりません(URL機種スペック解析): {raw_text}")
            return None
        parsed = json.loads(raw_text[json_start:json_end])
        if source_url:
            parsed["source_url"] = source_url
        return parsed
    except (KeyError, IndexError) as e:
        logger.error(f"Geminiレスポンス構造エラー(URL機種スペック解析): {e}")
    except json.JSONDecodeError as e:
        logger.error(f"JSON解析エラー(URL機種スペック解析): {e} / raw={raw_text!r}")
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
    return None, {
        "hint_words": [], "game_flow": "", "extra_info": "", "source_url": "",
        "basic_info": {}, "setting_ratios": {}, "ceiling_info": {},
        "bonus_info": {}, "cz_info": {}, "suggestion_info": {},
    }


def _format_structured_field(value, empty_label="登録なし"):
    """
    load_machine_rules() が返す辞書(または {"raw": "..."} 形式)を
    AIプロンプト・画面表示用の読みやすいテキストに変換する汎用フォーマッタ。
    (basic_info / ceiling_info / bonus_info / cz_info / suggestion_info 向け)
    """
    if not value:
        return empty_label
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        if set(value.keys()) == {"raw"}:
            return str(value["raw"])
        lines = []
        for k, v in value.items():
            if isinstance(v, dict):
                sub = ", ".join(f"{sk}:{sv}" for sk, sv in v.items())
                lines.append(f"{k} → {sub}")
            else:
                lines.append(f"{k}: {v}")
        return " / ".join(lines) if lines else empty_label
    return str(value)


def _format_setting_ratios(setting_ratios):
    """
    設定別確率表を読みやすいテキストに変換する。
    {"1": {"big": "1/398", ...}, ...} のような構造化データの他、
    スプレッドシートに直接書かれた自由記述テキスト({"raw": "..."} や文字列)にも対応する。
    """
    if not setting_ratios:
        return "登録なし"
    if isinstance(setting_ratios, str):
        return setting_ratios
    if isinstance(setting_ratios, dict) and set(setting_ratios.keys()) == {"raw"}:
        return str(setting_ratios["raw"])
    lines = []
    for setting_no in sorted(setting_ratios.keys(), key=lambda x: (len(x), x)):
        values = setting_ratios[setting_no]
        if isinstance(values, dict):
            parts = ", ".join(f"{k}:{v}" for k, v in values.items())
        else:
            parts = str(values)
        lines.append(f"設定{setting_no} → {parts}")
    return " / ".join(lines) if lines else "登録なし"


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
             recent_records_count=0):
    """
    machine_name・強示唆ワード・ゲームフロー・設定別確率表・累計データ・
    過去のメモやAI備考・同機種の直近の来店データ(とその傾向分析)をGeminiに渡し、
    設定1〜6それぞれの確率(%)と、日本語の短い判定コメントを生成してもらう。
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
    ceiling_info = rule.get("ceiling_info", {})
    bonus_info = rule.get("bonus_info", {})
    cz_info = rule.get("cz_info", {})
    suggestion_info = rule.get("suggestion_info", {})
    extra_info = rule.get("extra_info", "")

    total_games = stats.get("total_games", 0)
    big_count = stats.get("big_count", 0)
    reg_count = stats.get("reg_count", 0)
    actual_big_rate = f"1/{total_games / big_count:.1f}" if big_count else "算出不可"
    actual_reg_rate = f"1/{total_games / reg_count:.1f}" if reg_count else "算出不可"
    data_volume = _describe_data_volume(
        total_games, recent_records_count, bool(combined_text and combined_text.strip())
    )

    prompt = f"""
    あなたはパチスロの設定判別をサポートするアシスタントです。
    以下の情報をもとに、この台の設定1〜設定6それぞれである確率(%)を推測し、
    必ず以下のJSON形式のみで出力してください。他の文章・前置き・記号は一切不要です。

    {{"setting_probabilities": {{"1": 0, "2": 0, "3": 0, "4": 0, "5": 0, "6": 0}}, "comment": "20〜40文字程度の日本語コメント"}}

    【機種名】{machine_name}
    【この機種の強示唆ワード】{", ".join(hint_words) if hint_words else "登録なし"}
    【この機種のゲームフロー(AT/ART仕様など)】{game_flow if game_flow else "登録なし"}
    【この機種の設定別確率表(スペック表より)】{_format_setting_ratios(setting_ratios)}
    【この機種の天井情報】{_format_structured_field(ceiling_info)}
    【この機種のボーナス関連情報】{_format_structured_field(bonus_info)}
    【この機種のCZ関連情報】{_format_structured_field(cz_info)}
    【この機種の設定示唆演出】{_format_structured_field(suggestion_info)}
    【この機種のその他の登録情報】{extra_info if extra_info else "登録なし"}
    【今回の累計データ】総回転数: {total_games}G, BIG: {big_count}回 (実測確率 {actual_big_rate}), REG: {reg_count}回 (実測確率 {actual_reg_rate}), 現在の回転数: {stats.get("current_games", 0)}G, 差枚: {stats.get("difference_slabs", 0)}枚
    【今回のメモ・AI画像解析結果の蓄積テキスト】{combined_text if combined_text.strip() else "情報なし"}
    【同機種・このホールでの直近(約7日以内)の傾向分析】{hall_tendency_text if hall_tendency_text else "傾向データなし"}
    【同機種の直近の来店データ(個別内訳・参考情報)】{recent_history_text if recent_history_text else "登録なし"}
    【現時点の情報量】{data_volume}

    setting_probabilities の6つの値は、合計がちょうど100になるように整数で出力してください。
    情報量が「乏しい」場合は、特定の設定に偏らせず16〜17%前後の均等に近い数値にしてください
    (=まだ判別材料が少ないことを数値そのもので表現してください)。
    情報量が「普通」「十分」で、実測確率のズレ・強示唆ワード・ホールの傾向などから
    高設定/低設定の可能性が読み取れる場合は、該当する設定に大きく偏らせて構いません。
    設定別確率表が登録されている場合は、実測確率と各設定の理論値を比較して
    最も近い設定帯の確率を高めに評価してください。
    「同機種・このホールでの直近の傾向分析」(平均差枚・プラス収支率・強示唆ワード出現頻度など)は、
    そのホールがこの台に対して高設定を使いやすいかどうかの実績を示す重要な材料なので、
    単なる免責事項として退けず、確率分布に積極的に反映してください。
    comment には判定の根拠となった主なポイントを簡潔に含めてください。
    """
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
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
