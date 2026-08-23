import csv
import io
import os
import json
import logging
import re
import time
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urlparse

import requests
import gspread
from google.oauth2.service_account import Credentials
from flask import flash

# --- ロギング設定 ---
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 軽量インメモリキャッシュ
# ---------------------------------------------------------------------------
# 記録一覧・機種マスタ・Q&A履歴はページを開くたびにスプレッドシートへ読みに行くと、
# データが増えるほど表示が重くなる主因になる。短いTTL(既定20秒)でキャッシュし、
# 自分の書き込み操作(save_record等)の直後は該当キーを即座に無効化することで、
# 「保存した内容がすぐ反映されない」という不整合を避けつつ読み込み回数を減らす。
_cache = {}
_CACHE_TTL_SECONDS = 20


def _cache_get(key):
    entry = _cache.get(key)
    if not entry:
        return None
    value, expires_at = entry
    if time.time() > expires_at:
        del _cache[key]
        return None
    return value


def _cache_set(key, value, ttl=_CACHE_TTL_SECONDS):
    _cache[key] = (value, time.time() + ttl)


def _cache_invalidate(key):
    _cache.pop(key, None)

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
    "max_difference_slabs", "hamari_600_plus", "hamari_800_plus",
    "max_renchan", "graph_shape_tags", "category_scores", "suggestion_observations",
]

# machines シートの列構成
# keyword: 機種名に含まれるキーワード
# hint_words: 強示唆ワード群(カンマ区切り)
# game_flow: ゲームフロー・システムの説明(AT/ART純増、上乗せ契機など)
# setting_ratios: 設定1〜6ごとの確率(BIG/REG/合成など)をJSON文字列で格納
# sources: この機種データがどこから取り込まれたか(画像アップロード/URL)の履歴をJSON文字列で格納
MACHINE_HEADERS = ["keyword", "hint_words", "game_flow", "setting_ratios", "sources", "suggestion_items"]

# chat_logs シートの列構成(セッションごとのQ&A履歴)
CHAT_SHEET_NAME = os.environ.get("CHAT_SHEET_NAME", "chat_logs")
CHAT_HEADERS = ["session_id", "date", "question", "answer"]

# store_daily シートの列構成(店舗の日別データ)
# ホールデータサイトの「日付ごとの総差枚・平均差枚・平均G数・勝率」を貼り付けで取り込む。
# store_stats(店舗ごと1行の年間サマリー)と違い、1店舗×1日で1行になる。
STORE_DAILY_SHEET_NAME = os.environ.get("STORE_DAILY_SHEET_NAME", "store_daily")
STORE_DAILY_HEADERS = [
    "store_name", "date", "total_diff", "avg_diff", "avg_games", "win_rate",
    "win_units", "total_units", "source", "updated_at",
]

# store_units シートの列構成(店舗の台別データ)
# ホールデータサイトの日別詳細ページ(機種・台番号ごとのG数/差枚/BB/RB)を貼り付けで取り込む。
# 1店舗×1日×1台で1行になる。
STORE_UNITS_SHEET_NAME = os.environ.get("STORE_UNITS_SHEET_NAME", "store_units")
STORE_UNITS_HEADERS = [
    "store_name", "date", "machine_name", "machine_number",
    "total_games", "difference_slabs", "big_count", "reg_count",
    "source", "updated_at",
    # art_count は後から足した列。既存シートのヘッダーが前方一致のまま自動で移行できるよう、
    # 意味の並びとしては reg_count の隣が自然だが、あえて末尾に置いている。
    "art_count",
]

# store_events シートの列構成(店舗ごとの旧イベント日・周年日)
# 「毎月11日が強い」「9月9日が周年」といった店のクセを、日別データの集計に反映するために持つ。
# ルールは人が読める文字列のまま保存し、使うときに解析する
# (ルールの書き方を後から増やしても、シートを作り直さずに済むようにするため)
STORE_EVENTS_SHEET_NAME = os.environ.get("STORE_EVENTS_SHEET_NAME", "store_events")
STORE_EVENTS_HEADERS = [
    "store_name", "event_days", "anniversary_days", "note", "source", "updated_at",
]

# store_stats シートの列構成(店舗ごとの年間データ)
# データサイト等で公開されている「店舗単位の集計値」を1店舗1行で保持する。
# 自分の記録から作る集計(build_store_trends)とは別物なので、シートを分けている。
STORE_STATS_SHEET_NAME = os.environ.get("STORE_STATS_SHEET_NAME", "store_stats")
STORE_STATS_HEADERS = [
    "store_name", "period_label", "total_diff", "avg_diff", "avg_games", "win_rate",
    "note", "source", "updated_at",
]

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


def _ensure_min_columns(ws, needed_cols):
    """
    update_cell() は、シートの現在の列数(グリッドサイズ)を超える範囲には書き込めない
    (append_row と違って自動で列を広げてくれない)。
    ヘッダー移行で新しい列を追記する前に、必要な列数までシートを広げておく。
    """
    try:
        if ws.col_count < needed_cols:
            ws.add_cols(needed_cols - ws.col_count)
    except Exception as e:
        logger.warning(f"シートの列数拡張に失敗しました(そのまま続行します): {e}")


def get_records_worksheet():
    client = get_client()
    sheet = client.open_by_key(SPREADSHEET_ID)
    try:
        ws = sheet.worksheet(SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=SHEET_NAME, rows=1000, cols=len(HEADERS))
        ws.append_row(HEADERS)
        return ws

    current_headers = ws.row_values(1)
    if not current_headers:
        ws.append_row(HEADERS)
    elif current_headers != HEADERS and current_headers == HEADERS[:len(current_headers)]:
        # 列が後から追加された場合のみ、既存データをズラさずに不足ヘッダーだけ追記する
        _ensure_min_columns(ws, len(HEADERS))
        for i, header in enumerate(HEADERS[len(current_headers):], start=len(current_headers) + 1):
            ws.update_cell(1, i, header)
    elif current_headers != HEADERS:
        # 想定外のヘッダー構成の場合、insert_row で行をズラすと本番データが破損するため何もしない。
        logger.warning(
            f"記録データシートのヘッダーが想定と異なります: {current_headers} (期待値: {HEADERS})。"
            f"列がズレている可能性があるため、内容を確認してください。"
        )
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
        return ws

    current_headers = ws.row_values(1)
    if not current_headers:
        # ヘッダー行が空(真っさらなシート)の場合のみ、新規にヘッダー行を書き込む
        ws.append_row(MACHINE_HEADERS)
    elif current_headers != MACHINE_HEADERS and current_headers == MACHINE_HEADERS[:len(current_headers)]:
        # 既存ヘッダーが新ヘッダーの先頭部分と完全に一致する場合(=列が後から追加されただけ、
        # 例: sources列の新設)は、insert_row で行をズラさず、不足しているヘッダーだけを
        # 同じ1行目に追記する。insert_row を使うと既存データが1行分ズレて破損するため使わない。
        _ensure_min_columns(ws, len(MACHINE_HEADERS))
        for i, header in enumerate(MACHINE_HEADERS[len(current_headers):], start=len(current_headers) + 1):
            ws.update_cell(1, i, header)
    elif current_headers != MACHINE_HEADERS:
        # 想定外のヘッダー構成の場合は、データ破損を避けるためヘッダー行には手を加えない。
        # (get_all_records は実際のヘッダー行の文言をそのままキーとして使うため、
        #  多少キー名が古くても読み込み自体は継続できる)
        logger.warning(
            f"machinesシートのヘッダーが想定と異なります: {current_headers} "
            f"(期待値: {MACHINE_HEADERS})。列がズレている可能性があるため、内容を確認してください。"
        )
    return ws


def get_chat_worksheet():
    client = get_client()
    sheet = client.open_by_key(SPREADSHEET_ID)
    try:
        ws = sheet.worksheet(CHAT_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=CHAT_SHEET_NAME, rows=1000, cols=len(CHAT_HEADERS))
        ws.append_row(CHAT_HEADERS)
        return ws

    current_headers = ws.row_values(1)
    if not current_headers:
        ws.append_row(CHAT_HEADERS)
    elif current_headers != CHAT_HEADERS and current_headers == CHAT_HEADERS[:len(current_headers)]:
        # 列が後から追加された場合のみ、既存データをズラさずに不足ヘッダーだけ追記する
        _ensure_min_columns(ws, len(CHAT_HEADERS))
        for i, header in enumerate(CHAT_HEADERS[len(current_headers):], start=len(current_headers) + 1):
            ws.update_cell(1, i, header)
    elif current_headers != CHAT_HEADERS:
        logger.warning(
            f"chat_logsシートのヘッダーが想定と異なります: {current_headers} (期待値: {CHAT_HEADERS})"
        )
    return ws


def get_store_events_worksheet():
    client = get_client()
    sheet = client.open_by_key(SPREADSHEET_ID)
    try:
        ws = sheet.worksheet(STORE_EVENTS_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=STORE_EVENTS_SHEET_NAME, rows=200, cols=len(STORE_EVENTS_HEADERS))
        ws.append_row(STORE_EVENTS_HEADERS)
        return ws

    current_headers = ws.row_values(1)
    if not current_headers:
        ws.append_row(STORE_EVENTS_HEADERS)
    elif current_headers != STORE_EVENTS_HEADERS and current_headers == STORE_EVENTS_HEADERS[:len(current_headers)]:
        _ensure_min_columns(ws, len(STORE_EVENTS_HEADERS))
        for i, header in enumerate(STORE_EVENTS_HEADERS[len(current_headers):], start=len(current_headers) + 1):
            ws.update_cell(1, i, header)
    elif current_headers != STORE_EVENTS_HEADERS:
        logger.warning(
            f"store_eventsシートのヘッダーが想定と異なります: {current_headers} (期待値: {STORE_EVENTS_HEADERS})"
        )
    return ws


def get_store_stats_worksheet():
    client = get_client()
    sheet = client.open_by_key(SPREADSHEET_ID)
    try:
        ws = sheet.worksheet(STORE_STATS_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=STORE_STATS_SHEET_NAME, rows=200, cols=len(STORE_STATS_HEADERS))
        ws.append_row(STORE_STATS_HEADERS)
        return ws

    current_headers = ws.row_values(1)
    if not current_headers:
        ws.append_row(STORE_STATS_HEADERS)
    elif current_headers != STORE_STATS_HEADERS and current_headers == STORE_STATS_HEADERS[:len(current_headers)]:
        # 列が後から追加された場合のみ、既存データをズラさずに不足ヘッダーだけ追記する
        _ensure_min_columns(ws, len(STORE_STATS_HEADERS))
        for i, header in enumerate(STORE_STATS_HEADERS[len(current_headers):], start=len(current_headers) + 1):
            ws.update_cell(1, i, header)
    elif current_headers != STORE_STATS_HEADERS:
        logger.warning(
            f"store_statsシートのヘッダーが想定と異なります: {current_headers} (期待値: {STORE_STATS_HEADERS})"
        )
    return ws


def get_store_daily_worksheet():
    client = get_client()
    sheet = client.open_by_key(SPREADSHEET_ID)
    try:
        ws = sheet.worksheet(STORE_DAILY_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=STORE_DAILY_SHEET_NAME, rows=2000, cols=len(STORE_DAILY_HEADERS))
        ws.append_row(STORE_DAILY_HEADERS)
        return ws

    current_headers = ws.row_values(1)
    if not current_headers:
        ws.append_row(STORE_DAILY_HEADERS)
    elif current_headers != STORE_DAILY_HEADERS and current_headers == STORE_DAILY_HEADERS[:len(current_headers)]:
        _ensure_min_columns(ws, len(STORE_DAILY_HEADERS))
        for i, header in enumerate(STORE_DAILY_HEADERS[len(current_headers):], start=len(current_headers) + 1):
            ws.update_cell(1, i, header)
    elif current_headers != STORE_DAILY_HEADERS:
        logger.warning(
            f"store_dailyシートのヘッダーが想定と異なります: {current_headers} (期待値: {STORE_DAILY_HEADERS})"
        )
    return ws


def get_store_units_worksheet():
    client = get_client()
    sheet = client.open_by_key(SPREADSHEET_ID)
    try:
        ws = sheet.worksheet(STORE_UNITS_SHEET_NAME)
    except gspread.exceptions.WorksheetNotFound:
        ws = sheet.add_worksheet(title=STORE_UNITS_SHEET_NAME, rows=5000, cols=len(STORE_UNITS_HEADERS))
        ws.append_row(STORE_UNITS_HEADERS)
        return ws

    current_headers = ws.row_values(1)
    if not current_headers:
        ws.append_row(STORE_UNITS_HEADERS)
    elif current_headers != STORE_UNITS_HEADERS and current_headers == STORE_UNITS_HEADERS[:len(current_headers)]:
        _ensure_min_columns(ws, len(STORE_UNITS_HEADERS))
        for i, header in enumerate(STORE_UNITS_HEADERS[len(current_headers):], start=len(current_headers) + 1):
            ws.update_cell(1, i, header)
    elif current_headers != STORE_UNITS_HEADERS:
        logger.warning(
            f"store_unitsシートのヘッダーが想定と異なります: {current_headers} (期待値: {STORE_UNITS_HEADERS})"
        )
    return ws


NUMERIC_FIELDS = [
    "total_games", "big_count", "reg_count", "current_games", "difference_slabs",
    "max_difference_slabs", "hamari_600_plus", "hamari_800_plus", "max_renchan",
]


def _to_int(value):
    """スプレッドシートのセルが空文字や文字列で返ってきても安全にintへ変換する"""
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def _load_records_rows_fallback(ws):
    """
    machines側と同じ理由(get_all_records()がヘッダーの重複・空セルで例外を投げる)への対策。
    生の値からHEADERSの列位置を手動で特定して読み込む。
    """
    all_values = ws.get_all_values()
    if not all_values:
        return []
    header_row = all_values[0]
    col_index = {}
    for name in HEADERS:
        if name in header_row:
            col_index[name] = header_row.index(name)

    rows = []
    for raw_row in all_values[1:]:
        row_dict = {}
        for name, idx in col_index.items():
            row_dict[name] = raw_row[idx] if idx < len(raw_row) else ""
        if str(row_dict.get("session_id", "")).strip():  # session_idが空の行は除外
            rows.append(row_dict)
    return rows


def load_records():
    cached = _cache_get("records")
    if cached is not None:
        return cached

    try:
        ws = get_records_worksheet()
    except Exception as e:
        logger.error(f"スプレッドシート読み込みエラー(シート取得に失敗): {e}")
        return []

    try:
        records = ws.get_all_records()
    except Exception as e:
        logger.warning(
            f"get_all_records()に失敗したためフォールバック処理で読み込みます"
            f"(ヘッダー行の重複・空セルなどが原因の可能性): {e}"
        )
        try:
            records = _load_records_rows_fallback(ws)
        except Exception as fallback_error:
            logger.error(f"スプレッドシート読み込みエラー(フォールバックも失敗): {fallback_error}")
            return []

    try:
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
            raw_tags = str(r.get("graph_shape_tags", "")).strip()
            r["graph_shape_tags"] = [t.strip() for t in raw_tags.split(",") if t.strip()] if raw_tags else []
            raw_scores = str(r.get("category_scores", "")).strip()
            if raw_scores:
                try:
                    r["category_scores"] = json.loads(raw_scores)
                except json.JSONDecodeError:
                    r["category_scores"] = {}
            else:
                r["category_scores"] = {}
            raw_observations = str(r.get("suggestion_observations", "")).strip()
            if raw_observations:
                try:
                    r["suggestion_observations"] = json.loads(raw_observations)
                except json.JSONDecodeError:
                    r["suggestion_observations"] = {}
            else:
                r["suggestion_observations"] = {}
        records.reverse()  # 新しい順に表示
        _cache_set("records", records)
        return records
    except Exception as e:
        logger.error(f"スプレッドシート読み込みエラー(データ整形に失敗): {e}")
        return []


def save_record(record):
    try:
        ws = get_records_worksheet()
        row = [record.get(h, "") for h in HEADERS]
        ws.append_row(row)
        _cache_invalidate("records")
    except Exception as e:
        logger.error(f"スプレッドシート書き込みエラー: {e}")
        flash("スプレッドシートへの保存に失敗しました。")


def _load_machine_rows_fallback(ws):
    """
    ws.get_all_records() は、ヘッダー行に重複や空セルがあると例外を投げる
    (gspreadの既知の挙動)。過去のシート移行などでヘッダーが乱れている場合に
    「エラーは出ないが一覧が空に見える」事故につながるため、
    生の値を取得して期待するヘッダー名の列位置を手動で特定するフォールバックを用意する。
    """
    all_values = ws.get_all_values()
    if not all_values:
        return []
    header_row = all_values[0]
    col_index = {}
    for name in MACHINE_HEADERS:
        if name in header_row:
            col_index[name] = header_row.index(name)  # 同名が複数あれば最初の位置を採用

    rows = []
    for raw_row in all_values[1:]:
        row_dict = {}
        for name, idx in col_index.items():
            row_dict[name] = raw_row[idx] if idx < len(raw_row) else ""
        if str(row_dict.get("keyword", "")).strip():  # keywordが空の行(空行・ゴミ行)は除外
            rows.append(row_dict)
    return rows


def load_machine_rules():
    """
    machines シートから
    {keyword: {"hint_words": [...], "game_flow": "...", "setting_ratios": {...}}}
    の辞書を作る
    """
    cached = _cache_get("machine_rules")
    if cached is not None:
        return cached

    try:
        ws = get_machines_worksheet()
    except Exception as e:
        logger.error(f"機種マスタ読み込みエラー(シート取得に失敗): {e}")
        return {}

    try:
        rows = ws.get_all_records()
    except Exception as e:
        logger.warning(
            f"get_all_records()に失敗したためフォールバック処理で読み込みます"
            f"(ヘッダー行の重複・空セルなどが原因の可能性): {e}"
        )
        try:
            rows = _load_machine_rows_fallback(ws)
        except Exception as fallback_error:
            logger.error(f"機種マスタ読み込みエラー(フォールバックも失敗): {fallback_error}")
            return {}

    try:
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
            sources_raw = str(row.get("sources", "")).strip()
            if sources_raw:
                try:
                    sources = json.loads(sources_raw)
                    if not isinstance(sources, list):
                        sources = []
                except json.JSONDecodeError:
                    sources = []
            else:
                sources = []
            suggestion_items_raw = str(row.get("suggestion_items", "")).strip()
            if suggestion_items_raw:
                try:
                    suggestion_items = json.loads(suggestion_items_raw)
                    if not isinstance(suggestion_items, list):
                        suggestion_items = []
                except json.JSONDecodeError:
                    suggestion_items = []
            else:
                suggestion_items = []
            rules[keyword] = {
                "hint_words": hint_words,
                "game_flow": game_flow,
                "setting_ratios": setting_ratios,
                "sources": sources,
                "suggestion_items": suggestion_items,
            }
        _cache_set("machine_rules", rules)
        return rules
    except Exception as e:
        logger.error(f"機種マスタ読み込みエラー(データ整形に失敗): {e}")
        return {}


def get_records_sheet_diagnostics():
    """
    記録データ(records)シートの生の状態を確認するための軽量な診断情報。
    一覧が空に見えるときに、ユーザー自身がスプレッドシートを開かなくても
    画面上でシートの実際の中身(ヘッダー行・行数・先頭数行)を確認できるようにする。
    """
    try:
        ws = get_records_worksheet()
        all_values = ws.get_all_values()
        return {
            "ok": True,
            "total_rows": len(all_values),
            "header_row": all_values[0] if all_values else [],
            "sample_rows": all_values[1:6],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def get_machines_sheet_diagnostics():
    """
    machinesシートの生の状態を確認するための軽量な診断情報。
    一覧が空に見えるときに、ユーザー自身がスプレッドシートを開かなくても
    画面上でシートの実際の中身(ヘッダー行・行数・先頭数行)を確認できるようにする。
    """
    try:
        ws = get_machines_worksheet()
        all_values = ws.get_all_values()
        return {
            "ok": True,
            "total_rows": len(all_values),
            "header_row": all_values[0] if all_values else [],
            "sample_rows": all_values[1:6],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def save_machine_rule(keyword, hint_words, game_flow, setting_ratios, source_label=""):
    """
    machines シートに機種情報を保存する。
    同じ keyword の行が既にあれば、既存データに新しい内容を追記(マージ)する。
    なければ新規追加する。

    - hint_words: 既存 + 新規 を合算(重複除去)
    - game_flow: 既存の説明文の末尾に新しい説明文を追記(全く同じ内容なら追記しない)
    - setting_ratios: 既存の辞書をベースに、新しいキーで追加・更新(新規に無い既存キーは保持)
    - source_label: 今回取り込んだ情報源(画像アップロード/取り込み元URLなど)を示すラベル。
      既存の情報源リストに無ければ追加し、どのサイト・画像から情報を集約したかを蓄積していく。
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
        existing_sources = []
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
            if len(existing_row) > 4 and existing_row[4].strip():
                try:
                    parsed_sources = json.loads(existing_row[4])
                    if isinstance(parsed_sources, list):
                        existing_sources = parsed_sources
                except json.JSONDecodeError:
                    existing_sources = []

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

        # 情報源: 同じラベルが無ければ追加(取り込むたびに履歴として蓄積)
        merged_sources = list(existing_sources)
        source_label = (source_label or "").strip()
        if source_label:
            already_recorded = any(
                isinstance(s, dict) and s.get("label") == source_label for s in merged_sources
            )
            if not already_recorded:
                merged_sources.append({
                    "label": source_label,
                    "date": datetime.now().strftime("%Y-%m-%d"),
                })

        hint_words_str = ",".join(merged_hint_words)
        setting_ratios_json = json.dumps(merged_setting_ratios, ensure_ascii=False)
        sources_json = json.dumps(merged_sources, ensure_ascii=False)
        row_values = [keyword, hint_words_str, merged_game_flow, setting_ratios_json, sources_json]

        if target_row:
            ws.update(f"A{target_row}:E{target_row}", [row_values])
        else:
            ws.append_row(row_values)
        _cache_invalidate("machine_rules")
        return True
    except Exception as e:
        logger.error(f"機種マスタ書き込みエラー: {e}")
        return False


def add_machine_note(keyword, note):
    """
    既に登録済みの機種に、ユーザーが手動でメモ(特にゲームフロー・高設定挙動の示唆など)を
    追記するための関数。save_machine_rule() の game_flow マージ機構をそのまま使うので、
    既存のhint_words・setting_ratiosは変更せず、game_flowの末尾に新しい文章を追記するだけになる。
    """
    keyword = (keyword or "").strip()
    note = (note or "").strip()
    if not keyword or not note:
        return False
    return save_machine_rule(keyword, [], note, {}, source_label="手動メモ")


def _find_machine_row(ws, keyword):
    """keyword に完全一致する machines シートの行番号(1-indexed)を探す。無ければ None。"""
    existing_keywords = ws.col_values(1)
    for i, value in enumerate(existing_keywords[1:], start=2):
        if str(value).strip() == keyword:
            return i
    return None


def _load_suggestion_items_raw(ws, row):
    """指定行の suggestion_items(F列)を生のリストとして読み込む。"""
    existing_row = ws.row_values(row)
    if len(existing_row) > 5 and existing_row[5].strip():
        try:
            items = json.loads(existing_row[5])
            if isinstance(items, list):
                return items
        except json.JSONDecodeError:
            pass
    return []


def add_suggestion_item(keyword, name, item_type, weight):
    """
    示唆項目(アイキャッチ・トロフィー・穢れ解放・CZ確率など、機種固有の判別要素)を
    機種スペックに手動登録する。同名の項目が既にあれば上書き(種類・重みを更新)、
    無ければ追加する。

    keyword: 登録先の機種キーワード(完全一致、既存の機種である必要がある)
    name: 項目名(例:「ヤミアイキャッチ」「穢れ解放」)
    item_type: "count"(回数を入力する項目) または "boolean"(あり/なしの項目)
    weight: 判定における重要度(0〜100の整数。大きいほど設定判別への影響が強い項目として扱う)
    """
    keyword = (keyword or "").strip()
    name = (name or "").strip()
    if not keyword or not name:
        return False
    if item_type not in ("count", "boolean"):
        item_type = "count"
    try:
        weight = max(0, min(int(weight), 100))
    except (TypeError, ValueError):
        weight = 0

    try:
        ws = get_machines_worksheet()
        target_row = _find_machine_row(ws, keyword)
        if not target_row:
            logger.error(f"示唆項目追加エラー: 機種「{keyword}」が見つかりません")
            return False

        items = _load_suggestion_items_raw(ws, target_row)
        updated = False
        for item in items:
            if isinstance(item, dict) and item.get("name") == name:
                item["type"] = item_type
                item["weight"] = weight
                updated = True
                break
        if not updated:
            items.append({"name": name, "type": item_type, "weight": weight})

        _ensure_min_columns(ws, len(MACHINE_HEADERS))
        ws.update_cell(target_row, 6, json.dumps(items, ensure_ascii=False))
        _cache_invalidate("machine_rules")
        return True
    except Exception as e:
        logger.error(f"示唆項目追加エラー: {e}")
        return False


def remove_suggestion_item(keyword, name):
    """指定した機種から示唆項目を1件削除する。"""
    keyword = (keyword or "").strip()
    name = (name or "").strip()
    if not keyword or not name:
        return False
    try:
        ws = get_machines_worksheet()
        target_row = _find_machine_row(ws, keyword)
        if not target_row:
            return False

        items = _load_suggestion_items_raw(ws, target_row)
        new_items = [i for i in items if not (isinstance(i, dict) and i.get("name") == name)]

        _ensure_min_columns(ws, len(MACHINE_HEADERS))
        ws.update_cell(target_row, 6, json.dumps(new_items, ensure_ascii=False))
        _cache_invalidate("machine_rules")
        return True
    except Exception as e:
        logger.error(f"示唆項目削除エラー: {e}")
        return False


def find_mergeable_keyword(candidate_name, rules):
    """
    新しく登録しようとしている機種名(candidate_name、AIが画像/URLから読み取った名前)が、
    既存の登録キーワードと実質的に同じ機種を指していそうな場合、そのキーワードを返す。

    AIが機種名を読み取るたびに微妙に違う表記(例:「ToLOVEるダークネス」と
    「L ToLOVEるダークネス」)になることがあり、そのまま新規キーワードとして保存すると
    同じ機種のデータが複数のキーワードに分裂し、集約されなくなってしまう。
    これを避けるため、双方向の部分一致(どちらかがどちらかを含む)を許容し、
    最も一致度の高い(文字数が長い)既存キーワードを優先して返す。
    一致するものが無ければ None を返す(=新規キーワードとして登録する)。
    """
    candidate_name = (candidate_name or "").strip()
    if not candidate_name:
        return None

    for keyword in rules:
        if (keyword or "").strip() == candidate_name:
            return keyword  # 完全一致は即採用

    best_keyword = None
    best_overlap = 0
    for keyword in rules:
        k = (keyword or "").strip()
        if not k:
            continue
        if k in candidate_name or candidate_name in k:
            overlap = min(len(k), len(candidate_name))
            if overlap > best_overlap:
                best_overlap = overlap
                best_keyword = keyword
    return best_keyword


def load_all_chat_history():
    """
    chat_logs シートの全行を返す(スプレッドシートに追加された順=時系列順)。
    一覧画面で各セッションごとの質問件数・直近の回答をまとめて表示するために使う
    (セッションごとに毎回シートを読みに行くと遅くなるため、1回の読み込みで済ませる)。
    """
    cached = _cache_get("chat_history_all")
    if cached is not None:
        return cached
    try:
        ws = get_chat_worksheet()
        rows = ws.get_all_records()
        _cache_set("chat_history_all", rows)
        return rows
    except Exception as e:
        logger.error(f"チャット全履歴読み込みエラー: {e}")
        return []


def load_chat_history(session_id, limit=20):
    """
    指定セッションのQ&A履歴を古い順(=会話の時系列順)で返す。
    直近のやり取りのみをAIへの文脈として使うため、件数を limit で絞る。
    load_all_chat_history() のキャッシュを再利用し、二重にシートを読みに行かない。
    """
    session_id = str(session_id or "").strip()
    if not session_id:
        return []
    try:
        rows = load_all_chat_history()
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
        _cache_invalidate("chat_history_all")
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


def get_store_machine_history(store_name, machine_number, days=90, limit=60):
    """
    「同一店舗・同一台番号」の記録を日付昇順で返す(グラフ表示用)。
    店舗名・台番号のどちらかが空の場合は、物理的に同じ台かどうか特定できないため空リストを返す。

    get_recent_same_machine_records() と同じ方針で、同じ日に複数回記録している場合は
    その日の最も新しい1件のみを採用する(重複カウントを避けるため)。
    """
    store_name = (store_name or "").strip()
    machine_number = (machine_number or "").strip()
    if not store_name or not machine_number:
        return []

    records = load_records()  # 新しい順
    now = datetime.now()

    candidates = []
    for r in records:
        if str(r.get("store_name", "")).strip() != store_name:
            continue
        if str(r.get("machine_number", "")).strip() != machine_number:
            continue
        try:
            record_date = datetime.strptime(str(r.get("date", "")), "%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            continue
        if (now - record_date).days > days:
            continue
        candidates.append((record_date, r))

    # 同じ日の記録はその日の最新1件のみを残す
    latest_by_day = {}
    for record_date, r in candidates:
        date_only = record_date.strftime("%Y-%m-%d")
        existing = latest_by_day.get(date_only)
        if existing is None or record_date > existing[0]:
            latest_by_day[date_only] = (record_date, r)

    ordered = sorted(latest_by_day.values(), key=lambda x: x[0])  # 日付昇順(グラフ用)
    return [r for _, r in ordered[-limit:]]


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

    追加で、画像から読み取れる範囲で以下も抽出してください(いずれも画面に実際に数値として
    表示されている場合のみ読み取り、グラフの形から推測で数値を作らないこと):
    - max_difference_slabs: 最大差枚(グラフの最高点付近に数値表示があれば。無ければ0)
    - hamari_600_plus: 600G以上のハマり回数(ハマり回数の表・区間表示があれば。無ければ0)
    - hamari_800_plus: 800G以上のハマり回数(同上。無ければ0)
    - max_renchan: 最大連チャン数(BB/AT等の連続回数表示があれば。無ければ0)
    - graph_shape_tags: 差枚グラフ全体の形状について、以下の語彙から当てはまるものだけを
      配列で選ぶ(画像にグラフが写っていて、形状が明確に読み取れる場合のみ。不明なら空配列):
      ["右肩上がり","右肩下がり","横ばい","一撃型","後ヅモ型","尻上がり","前半型","乱高下","安定型"]
      これはグラフの座標を厳密に解析した数値ではなく、見た目からの大まかな分類である前提で選ぶこと。
    {machine_context}
    {{"total_games": 0, "big_count": 0, "reg_count": 0, "current_games": 0, "difference_slabs": 0,
      "machine_number": "", "graph_features": "", "other_info": "",
      "max_difference_slabs": 0, "hamari_600_plus": 0, "hamari_800_plus": 0,
      "max_renchan": 0, "graph_shape_tags": []}}
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
def build_dashboard_stats(history):
    """
    データ管理ダッシュボード上部に表示する簡単な統計サマリーを計算する。
    今後この手のサマリー項目を増やす場合はここに追記していく。
    """
    total_records = len(history)
    unique_machines = len({str(r.get("machine_name", "")).strip() for r in history if str(r.get("machine_name", "")).strip()})
    unique_sessions = len({str(r.get("session_id", "")).strip() for r in history if str(r.get("session_id", "")).strip()})

    today_str = datetime.now().strftime("%Y-%m-%d")
    today_count = sum(1 for r in history if str(r.get("date", "")).startswith(today_str))

    return {
        "total_records": total_records,
        "unique_machines": unique_machines,
        "unique_sessions": unique_sessions,
        "today_count": today_count,
    }


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


def debug_machine_name_match(machine_name):
    """
    machine_name が machines シートのどのキーワードとマッチする/しないかを診断する。
    「登録したはずなのに一致しない」という問題の原因(前後の空白・全角/半角の違い・
    見えない文字など)を切り分けるためのデバッグ用関数。

    戻り値: {
        "input": 入力された機種名(前後空白除去前後の両方を表示),
        "matched_keyword": 最終的に採用されたキーワード(無ければ None),
        "candidates": [{"keyword": ..., "is_exact_match": bool, "is_substring_match": bool}, ...],
    }
    """
    raw_input = machine_name or ""
    stripped_input = raw_input.strip()
    rules = load_machine_rules()

    candidates = []
    for keyword in rules.keys():
        k = (keyword or "").strip()
        candidates.append({
            "keyword": keyword,
            "keyword_repr": repr(keyword),  # 前後の見えない空白・改行などがあれば repr で分かる
            "is_exact_match": bool(k) and k == stripped_input,
            "is_substring_match": bool(k) and k in stripped_input,
        })

    matched_keyword, _ = find_machine_rule(machine_name)

    return {
        "input": raw_input,
        "input_repr": repr(raw_input),
        "stripped_input": stripped_input,
        "matched_keyword": matched_keyword,
        "candidates": candidates,
    }


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


def _estimate_payout_rate(total_games, difference_slabs, coins_per_game=3):
    """
    総回転数と差枚から、おおまかな出率(機械割)をPython側で計算する(⑧出率予測)。
    1Gあたりの投入枚数(coins_per_game)は主流の3枚がけを既定値とする。
    実際の投入枚数はBIG/RB中の増減や小役ズレなどで多少前後するため、あくまで目安。
    """
    if total_games <= 0:
        return None
    coin_in = total_games * coins_per_game
    if coin_in <= 0:
        return None
    coin_out = coin_in + difference_slabs
    return coin_out / coin_in * 100


CATEGORY_SCORE_LABELS = {
    "big_reg_match": ("合算一致", 25),
    "suggestion_items": ("示唆項目", 25),
    "graph_pattern": ("グラフ", 15),
    "hamari": ("ハマり", 10),
    "renchan": ("連チャン", 10),
    "hall_tendency": ("ホール傾向", 15),
}


def _normalize_category_scores(category_scores):
    """
    Geminiが返したカテゴリ別スコアを {key: 0〜満点の整数} の辞書に整形する。
    不正な値・キーは除外し、各スコアは満点でクリップする。
    テンプレート表示用の詳細(ラベル・満点)は describe_category_scores() で付与する。
    """
    if not isinstance(category_scores, dict) or not category_scores:
        return {}
    normalized = {}
    for key, (_, max_score) in CATEGORY_SCORE_LABELS.items():
        if key not in category_scores:
            continue
        try:
            score = int(category_scores[key])
        except (TypeError, ValueError):
            continue
        normalized[key] = max(0, min(score, max_score))
    return normalized


def describe_category_scores(category_scores):
    """
    {"big_reg_match": 24, ...} のような辞書を、画面表示用に
    ラベル・満点・達成率も含めたリストに変換する(スコア内訳UI用)。
    スコアが無い場合は空リストを返す(=呼び出し側でスコア内訳セクション自体を出し分けられる)。
    """
    if not isinstance(category_scores, dict) or not category_scores:
        return []
    result = []
    for key, (label, max_score) in CATEGORY_SCORE_LABELS.items():
        if key not in category_scores:
            continue
        try:
            score = int(category_scores[key])
        except (TypeError, ValueError):
            continue
        score = max(0, min(score, max_score))
        result.append({
            "key": key,
            "label": label,
            "score": score,
            "max_score": max_score,
            "pct": round(score / max_score * 100) if max_score else 0,
        })
    return result


def category_scores_total(category_scores):
    """スコア内訳の合計(現在値, 満点)を返す。スコアが無ければ (0, 0)。"""
    breakdown = describe_category_scores(category_scores)
    if not breakdown:
        return 0, 0
    return sum(b["score"] for b in breakdown), sum(b["max_score"] for b in breakdown)


def _format_suggestion_observations(suggestion_items, suggestion_observations):
    """
    機種に登録されている示唆項目(アイキャッチ・トロフィー・穢れ解放など)の定義と、
    今回の記録での観測値を突き合わせて、プロンプト用のテキストに整形する。
    観測値が無い項目は「未入力」として明示し、AIが「0だから無かった」と
    誤解しないようにする(入力自体を忘れている可能性があるため)。
    """
    if not suggestion_items:
        return "この機種には示唆項目が登録されていません(機種スペック登録システムから追加できます)"

    lines = []
    for item in suggestion_items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        weight = item.get("weight", 0)
        item_type = item.get("type", "count")
        observed = suggestion_observations.get(name)
        if observed is None or observed == "":
            observed_text = "未入力"
        elif item_type == "boolean":
            observed_text = "あり" if str(observed) in ("1", "true", "True", "on") else "なし"
        else:
            observed_text = f"{observed}回"
        lines.append(f"・{name}(重要度{weight}/100): {observed_text}")
    return "\n".join(lines) if lines else "この機種には示唆項目が登録されていません"


def estimate(machine_name, combined_text, stats=None, recent_history_text="", hall_tendency_text="",
             recent_records_count=0, base64_image=None, mime_type="image/jpeg", suggestion_observations=None,
             store_stats_text=""):
    """
    machine_name・強示唆ワード・ゲームフロー・設定別確率表・累計データ・
    過去のメモやAI備考・同機種の直近の来店データ(とその傾向分析)・
    機種固有の示唆項目の観測結果(アイキャッチ・トロフィー・穢れ解放など)をGeminiに渡し、
    設定1〜6それぞれの確率(%)と、日本語の短い判定コメントを生成してもらう。
    base64_image が渡された場合は、今回アップロードされたデータ画面・グラフ画像も
    そのまま添付し、AIに画像を直接見た上で判定させる(グラフの形状や画面内の
    示唆演出など、テキスト化しきれていない情報を判定材料に加えるため)。
    また、スペック表(設定別のBIG/REG理論値)と実測値の乖離をPython側で事前計算し、
    ヒントとしてプロンプトに含めることで、AIが分数の比較を誤るリスクを減らしている。
    情報が少ない場合は、確率が特定の設定に偏らず均等に近い数値になるよう指示している
    (=数値そのものが「まだ判別材料が少ない」ことを表す)。
    AI呼び出しに失敗した場合は簡易的なキーワード判定にフォールバックする。

    戻り値: (comment: str, setting_probabilities: {"1": int, ..., "6": int}, category_scores: dict)
    """
    stats = stats or {}
    suggestion_observations = suggestion_observations or {}
    matched_keyword, rule = find_machine_rule(machine_name)
    hint_words = rule.get("hint_words", [])
    game_flow = rule.get("game_flow", "")
    setting_ratios = rule.get("setting_ratios", {})
    suggestion_items = rule.get("suggestion_items", [])
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
    suggestion_items_text = _format_suggestion_observations(suggestion_items, suggestion_observations)

    difference_slabs = stats.get("difference_slabs", 0)
    max_difference_slabs = stats.get("max_difference_slabs", 0) or difference_slabs
    hamari_600_plus = stats.get("hamari_600_plus", 0)
    hamari_800_plus = stats.get("hamari_800_plus", 0)
    max_renchan = stats.get("max_renchan", 0)
    graph_shape_tags = stats.get("graph_shape_tags", [])
    payout_rate = _estimate_payout_rate(total_games, difference_slabs)
    payout_rate_text = f"約{payout_rate:.1f}%(3枚がけ換算の目安、実際の投入枚数とはズレる場合あり)" if payout_rate is not None else "算出不可"

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
    以下の情報をもとに、この台の設定1〜設定6それぞれである確率(%)を推測してください。
    判定にあたっては、以下6つのカテゴリごとに根拠の強さを点数化してから、
    総合的に設定確率を決めてください(点数配分の目安: 合算確率の一致度25点、
    示唆項目(アイキャッチ・トロフィー・穢れ解放など機種固有の判別要素)25点、
    差枚グラフの推移15点、ハマり回数10点、連チャン10点、ホールの傾向15点、合計100点。
    データが無い/薄いカテゴリは低めの点数にし、無理に高得点にしないこと。
    特に「示唆項目」は、実戦で最も重視される要素なので、観測結果があれば重要度に応じて
    しっかり点数に反映してください)。

    必ず以下のJSON形式のみで出力してください。他の文章・前置き・記号は一切不要です。

    {{"category_scores": {{"big_reg_match": 0, "suggestion_items": 0, "graph_pattern": 0, "hamari": 0, "renchan": 0, "hall_tendency": 0}},
      "setting_probabilities": {{"1": 0, "2": 0, "3": 0, "4": 0, "5": 0, "6": 0}},
      "comment": "20〜40文字程度の日本語コメント"}}

    【機種名】{machine_name}
    【この機種の強示唆ワード】{", ".join(hint_words) if hint_words else "登録なし"}
    【この機種のゲームフロー(AT/ART仕様など)】{game_flow if game_flow else "登録なし"}
    【この機種の設定別確率表(スペック表より)】{_format_setting_ratios(setting_ratios)}
    【実測値と設定別理論値の自動比較(Pythonで計算済み、参考値として重視してください)】{setting_match_hint}
    【示唆項目の観測結果(機種固有・重要度付き、実戦で最も重視される情報)】
    {suggestion_items_text}
    【今回の累計データ】総回転数: {total_games}G, BIG: {big_count}回 (実測確率 {actual_big_rate}), REG: {reg_count}回 (実測確率 {actual_reg_rate}), 現在の回転数: {stats.get("current_games", 0)}G
    【差枚グラフ関連】最終差枚: {difference_slabs}枚, 最大差枚: {max_difference_slabs}枚, 推定出率: {payout_rate_text}
    【グラフの形状(画像からのAIによる大まかな分類、正確な数値ではない参考情報)】{", ".join(graph_shape_tags) if graph_shape_tags else "情報なし"}
    【ハマり回数】600G以上: {hamari_600_plus}回, 800G以上: {hamari_800_plus}回 (画面に表示があった場合のみ。0は「表示なし/未検出」の可能性もある)
    【最大連チャン】{max_renchan}連 (画面に表示があった場合のみ。0は「表示なし/未検出」の可能性もある)
    【今回のメモ・AI画像解析結果の蓄積テキスト】{combined_text if combined_text.strip() else "情報なし"}
    【同機種・このホールでの直近(約7日以内)の傾向分析】{hall_tendency_text if hall_tendency_text else "傾向データなし"}
    【同機種の直近の来店データ(個別内訳・参考情報)】{recent_history_text if recent_history_text else "登録なし"}
    【このホール全体の年間データ(データサイト等の外部集計。店の全台・全期間が対象の実績値で、
    その店がどれくらい出す方針かの目安。個々の台の設定を直接示すものではないため、
    「ホールの傾向」カテゴリの点数付けの材料として扱い、これだけで高設定と判断しないこと)】{store_stats_text if store_stats_text else "登録なし"}
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
    ハマり回数・連チャン数が「0」の場合、必ずしも「ハマりが無かった/連チャンが無かった」
    ことを意味せず、単に画面に表示が無くて読み取れなかった可能性もあるため、
    0の場合はそのカテゴリの点数を高くも低くもせず中間程度にとどめてください。
    示唆項目が「未入力」の場合も同様に、「発生しなかった」とは断定せず中間程度の点数にしてください。
    重要度(重み)が高い項目が観測されている場合は、それだけで高設定側に大きく傾けて構いません
    (アイキャッチ・トロフィー・穢れ解放などの示唆項目は、実戦において最も信頼性の高い判別要素です)。
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
            category_scores = _normalize_category_scores(parsed.get("category_scores"))
            return comment, probabilities, category_scores
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
        return "高設定濃厚!? (要確認/AI判定失敗のため簡易判定)", fallback_probabilities, {}
    return "推測中...(AI判定失敗)", _normalize_setting_probabilities({}), {}


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


# ---------------------------------------------------------------------------
# 店舗の傾向分析
# ---------------------------------------------------------------------------
# 「この店はいつ・どの機種に設定を入れているか」を、蓄積した記録から集計する。
# 判定の材料はあくまで自分が打った記録だけなので、件数が少ない区分は
# 平均差枚が大きく振れる。UI側で件数を必ず併記し、少数データを
# 「傾向」と誤読しないようにすること。
WEEKDAY_LABELS = ["月", "火", "水", "木", "金", "土", "日"]

# この件数以上ある区分だけを「注目ポイント」として拾う(少数データのブレ対策)
TREND_MIN_SAMPLES = 3


def parse_record_datetime(record):
    """記録の date 列を datetime に変換する(壊れている行は None)。"""
    raw = str(record.get("date", "")).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt)
        except (ValueError, TypeError):
            continue
    return None


def list_store_names(records=None):
    """
    記録に登場する店舗名を、記録件数の多い順に返す。
    戻り値: [{"name": 店舗名, "count": 件数}, ...]
    """
    if records is None:
        records = load_records()

    counts = {}
    for r in records:
        name = str(r.get("store_name", "")).strip()
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1

    return [
        {"name": name, "count": count}
        for name, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _dedupe_daily_records(records):
    """
    「同一台番号・同じ日」の記録は、その日の最新1件だけを残す。
    (同じ台を1日に複数回記録している場合、そのままだと同じ台の結果を
    二重に数えてしまい、平均差枚が実態からずれるため。
    台番号が未入力の記録は、機種名+日付でまとめる簡易対応とする。)
    """
    latest_by_group = {}
    for record_dt, r in records:
        machine_number = str(r.get("machine_number", "")).strip()
        machine_name = str(r.get("machine_name", "")).strip()
        key = (machine_number or f"name:{machine_name}", record_dt.strftime("%Y-%m-%d"))
        existing = latest_by_group.get(key)
        if existing is None or record_dt > existing[0]:
            latest_by_group[key] = (record_dt, r)
    return sorted(latest_by_group.values(), key=lambda x: x[0], reverse=True)


def _summarize_group(records):
    """記録のかたまり(1グループ)の平均差枚・プラス率などを計算する。"""
    n = len(records)
    if n == 0:
        return None
    diffs = [_to_int(r.get("difference_slabs", 0)) for r in records]
    games = [_to_int(r.get("total_games", 0)) for r in records]
    plus_count = sum(1 for d in diffs if d > 0)
    return {
        "count": n,
        "avg_diff": sum(diffs) / n,
        "total_diff": sum(diffs),
        "best_diff": max(diffs),
        "worst_diff": min(diffs),
        "plus_count": plus_count,
        "plus_rate": plus_count / n * 100,
        "avg_games": sum(games) / n,
        "enough_samples": n >= TREND_MIN_SAMPLES,
    }


def _grouped_summaries(pairs, key_func, label_func=None, sort_key=None):
    """
    (datetime, record) のリストを key_func でグルーピングし、区分ごとの集計を返す。
    key_func が None を返した記録はその集計から除外する(台番号未入力など)。
    """
    groups = {}
    for record_dt, r in pairs:
        key = key_func(record_dt, r)
        if key is None:
            continue
        groups.setdefault(key, []).append(r)

    rows = []
    for key, records in groups.items():
        summary = _summarize_group(records)
        summary["key"] = key
        summary["label"] = label_func(key) if label_func else str(key)
        rows.append(summary)

    if sort_key:
        rows.sort(key=sort_key)
    else:
        rows.sort(key=lambda row: -row["avg_diff"])
    return rows


def _pick_highlight(rows, best=True):
    """
    件数が十分ある区分の中から、平均差枚が最も高い(または低い)ものを1つ返す。
    十分な件数の区分が無ければ None(「傾向あり」と言い切れないため)。
    """
    eligible = [row for row in rows if row["enough_samples"]]
    if not eligible:
        return None
    return max(eligible, key=lambda row: row["avg_diff"]) if best else min(eligible, key=lambda row: row["avg_diff"])


def build_store_trends(store_name, days=90):
    """
    1店舗ぶんの記録を、日別・曜日別・日付末尾別・機種別・台番号末尾別に集計する。

    days: 何日ぶんの記録を対象にするか(0以下なら全期間)
    戻り値: 集計結果の辞書。対象の記録が1件も無い場合は record_count=0 の辞書を返す。
    """
    store_name = (store_name or "").strip()
    if not store_name:
        return None

    now = datetime.now()
    pairs = []
    for r in load_records():
        if str(r.get("store_name", "")).strip() != store_name:
            continue
        record_dt = parse_record_datetime(r)
        if record_dt is None:
            continue
        if days > 0 and (now - record_dt).days > days:
            continue
        pairs.append((record_dt, r))

    pairs = _dedupe_daily_records(pairs)
    records = [r for _, r in pairs]

    trends = {
        "store_name": store_name,
        "days": days,
        "record_count": len(records),
        "overall": _summarize_group(records),
        "min_samples": TREND_MIN_SAMPLES,
    }
    if not records:
        return trends

    trends["first_date"] = min(dt for dt, _ in pairs).strftime("%Y-%m-%d")
    trends["last_date"] = max(dt for dt, _ in pairs).strftime("%Y-%m-%d")
    trends["unique_days"] = len({dt.strftime("%Y-%m-%d") for dt, _ in pairs})

    # 日別: 新しい日付が上に来るように並べる(イベント日の当たりを探す用途)
    trends["by_date"] = _grouped_summaries(
        pairs,
        key_func=lambda dt, r: dt.strftime("%Y-%m-%d"),
        sort_key=lambda row: row["key"],
    )
    trends["by_date"].reverse()

    # 曜日別: 月〜日の並びを固定する
    trends["by_weekday"] = _grouped_summaries(
        pairs,
        key_func=lambda dt, r: dt.weekday(),
        label_func=lambda key: f"{WEEKDAY_LABELS[key]}曜",
        sort_key=lambda row: row["key"],
    )

    # 日付末尾別: 「毎月7の付く日」のような周期イベントを探す用途
    trends["by_day_suffix"] = _grouped_summaries(
        pairs,
        key_func=lambda dt, r: dt.day % 10,
        label_func=lambda key: f"末尾{key}の日",
        sort_key=lambda row: row["key"],
    )

    # 機種別: その店がどの機種に設定を入れているかを見る用途
    trends["by_machine"] = _grouped_summaries(
        pairs,
        key_func=lambda dt, r: str(r.get("machine_name", "")).strip() or None,
    )

    # 台番号末尾別: 台番号が入力されている記録のみが対象
    def _number_suffix(dt, r):
        number = str(r.get("machine_number", "")).strip()
        return int(number[-1]) if number.isdigit() else None

    trends["by_number_suffix"] = _grouped_summaries(
        pairs,
        key_func=_number_suffix,
        label_func=lambda key: f"末尾{key}",
        sort_key=lambda row: row["key"],
    )

    # ゾロ目台(111・222など、桁がすべて同じ台番号)は島の角や看板台として
    # 扱われることがあるため、末尾別とは別枠で集計する
    def _repdigit(dt, r):
        number = str(r.get("machine_number", "")).strip()
        if number.isdigit() and len(number) >= 2 and len(set(number)) == 1:
            return "repdigit"
        return None

    repdigit_rows = _grouped_summaries(pairs, key_func=_repdigit, label_func=lambda key: "ゾロ目台")
    trends["repdigit"] = repdigit_rows[0] if repdigit_rows else None

    # 注目ポイント: 件数が十分ある区分だけから拾う
    trends["highlights"] = {
        "best_weekday": _pick_highlight(trends["by_weekday"]),
        "worst_weekday": _pick_highlight(trends["by_weekday"], best=False),
        "best_day_suffix": _pick_highlight(trends["by_day_suffix"]),
        "best_machine": _pick_highlight(trends["by_machine"]),
        "worst_machine": _pick_highlight(trends["by_machine"], best=False),
        "best_number_suffix": _pick_highlight(trends["by_number_suffix"]),
    }
    return trends


def _describe_trend_rows(title, rows, limit=10):
    """集計結果をAIプロンプト用の1行テキストにする。"""
    if not rows:
        return f"【{title}】データなし"
    parts = [
        f"{row['label']}: 平均差枚{row['avg_diff']:+.0f}枚/{row['count']}件/プラス率{row['plus_rate']:.0f}%"
        for row in rows[:limit]
    ]
    return f"【{title}】" + " / ".join(parts)


def describe_store_trends(trends):
    """build_store_trends() の結果を、AIに渡す・ログに残すためのテキストにまとめる。"""
    if not trends or not trends.get("record_count"):
        return "対象の記録がありません。"

    overall = trends["overall"]
    lines = [
        f"【店舗】{trends['store_name']}",
        f"【対象期間】{trends.get('first_date', '')}〜{trends.get('last_date', '')} "
        f"(記録{trends['record_count']}件 / 実際に打った日数{trends.get('unique_days', 0)}日)",
        f"【全体】平均差枚{overall['avg_diff']:+.0f}枚 / プラス率{overall['plus_rate']:.0f}% "
        f"({overall['plus_count']}/{overall['count']}回) / 平均{overall['avg_games']:.0f}G",
        _describe_trend_rows("日別(直近)", trends.get("by_date", []), limit=14),
        _describe_trend_rows("曜日別", trends.get("by_weekday", [])),
        _describe_trend_rows("日付末尾別", trends.get("by_day_suffix", [])),
        _describe_trend_rows("機種別", trends.get("by_machine", []), limit=12),
        _describe_trend_rows("台番号末尾別", trends.get("by_number_suffix", [])),
    ]
    repdigit = trends.get("repdigit")
    if repdigit:
        lines.append(
            f"【ゾロ目台】平均差枚{repdigit['avg_diff']:+.0f}枚/{repdigit['count']}件/"
            f"プラス率{repdigit['plus_rate']:.0f}%"
        )
    return "\n".join(lines)


def summarize_store_trends_with_gemini(trends, store_stats=None, daily_trends=None, unit_trends=None):
    """
    集計結果をもとに、店舗の傾向についてのコメントをAIに書いてもらう。

    注意: 元データは「自分が打った記録」だけなので、母数が小さい区分は
    偶然のブレである可能性が高い。プロンプト側でもその点を明示し、
    断定させないようにしている。
    store_stats(店舗全体の年間データ)がある場合は、母数の大きい実績として
    併せて渡し、自分の記録との差にも触れてもらう。
    戻り値: (summary: str, error: str)
    """
    if not trends or not trends.get("record_count"):
        return "", "集計対象の記録がありません。"

    prompt = f"""
    あなたはパチスロのホール取材・データ分析に詳しいアナリストです。
    以下は、あるユーザーが1つの店舗で自分が打った台の記録だけを集計したものです。
    このデータから読み取れる「その店舗の傾向」を日本語で説明してください。

    重要な前提:
    - これは店の全台データではなく、ユーザーが打った台のみの偏ったサンプルです。
    - 件数({trends['min_samples']}件未満)が少ない区分は偶然のブレの可能性が高いので、
      傾向として断定せず「サンプルが少ない」と明記してください。
    - 存在しない数値を作らず、以下のデータに書かれている数値だけを使ってください。

    出力形式(プレーンテキスト、箇条書き3〜5行、合計300文字程度):
    - 全体の傾向を1行
    - 狙い目になりそうな日・曜日・機種・台番号の傾向を2〜3行(根拠の数値を添えて)
    - 判断に注意が必要な点(サンプル数の偏りなど)を1行

    {describe_store_trends(trends)}

    【店舗全体の年間データ(データサイト等の外部集計。店の全台が対象なので、
    上のユーザー自身の記録より母数がはるかに大きい実績値)】
    {describe_store_stats(store_stats)}

    【取り込んだホールデータの日別集計(店の全台が対象。平均G数=稼働の高さで、
    全体比が高い曜日・日付はイベント日である可能性がある。差枚・勝率は
    サイト側で空欄の日が多く、有効日数が少ない項目は参考程度に扱うこと)】
    {describe_store_daily_trends(daily_trends)}

    【取り込んだ台別データの集計(店の全台が対象。台番号末尾や「端台(角台の候補)」ごとの
    平均差枚。端台の判定は、同じ日・同じ機種の台番号の連番の両端という近似なので、
    実際の島の角とはズレることがある点に注意)】
    {describe_store_unit_trends(unit_trends)}

    年間データやホールデータが登録されている場合は、店全体の水準(稼働・平均差枚・勝率)と、
    ユーザー自身の記録の水準がどれくらいズレているかにも1行触れてください。
    台別データがある場合は、狙う価値のありそうな台番号の特徴(末尾・端台・機種)にも1行触れてください。
    """
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    headers = {"Content-Type": "application/json"}

    try:
        response = requests.post(
            GEMINI_URL, headers=headers, data=json.dumps(payload), timeout=REQUEST_TIMEOUT
        )
        response.raise_for_status()
        text = response.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        if not text:
            return "", "AIからの回答が空でした。もう一度お試しください。"
        return text, ""
    except requests.exceptions.Timeout:
        logger.error("店舗傾向AI総評: タイムアウト")
        return "", "AIの応答がタイムアウトしました。もう一度お試しください。"
    except requests.exceptions.RequestException as e:
        logger.error(f"店舗傾向AI総評: 通信エラー: {e}")
        return "", "AIとの通信に失敗しました。時間をおいてお試しください。"
    except (KeyError, IndexError, ValueError, TypeError) as e:
        logger.error(f"店舗傾向AI総評: 解析エラー: {e}")
        return "", "AIの回答を解析できませんでした。もう一度お試しください。"


# ---------------------------------------------------------------------------
# 店舗の年間データ(データサイト等の外部集計)の登録・読み込み
# ---------------------------------------------------------------------------
# build_store_trends() が「自分が打った台の記録」から作る集計なのに対し、
# こちらは店舗全体の年間実績(総差枚・平均差枚・平均G数・勝率)を外部から取り込んで持つ。
# 母数が桁違いなので、混ぜずに別データとして扱い、画面でも並べて比較する。
def _to_number(value, default=None):
    """
    "+12,345枚" "58.3%" "1,234G" のような表記から数値だけを取り出す。
    数値として解釈できない場合は default を返す。
    """
    if value is None:
        return default
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value).strip()
    if not text:
        return default
    # 数字・符号・小数点以外(カンマ、枚、%、G、全角文字など)を取り除く
    cleaned = re.sub(r"[^0-9+\-.]", "", text.replace("−", "-").replace("＋", "+"))
    if cleaned in ("", "+", "-", ".", "+.", "-."):
        return default
    try:
        return float(cleaned)
    except ValueError:
        return default


def parse_number(value, default=None):
    """フォーム入力などの数値表記を数値に変換する(_to_number の公開版)。"""
    return _to_number(value, default)


def load_store_stats():
    """
    store_stats シートを読み込み、{店舗名: 年間データ} の辞書で返す。
    シートが無い・読めない場合は空辞書を返す(この機能が使えないだけで、
    他の画面は今まで通り動かしたいため、例外は投げない)。
    """
    cached = _cache_get("store_stats")
    if cached is not None:
        return cached

    try:
        ws = get_store_stats_worksheet()
        rows = ws.get_all_records()
    except Exception as e:
        logger.error(f"店舗年間データの読み込みエラー: {e}")
        return {}

    stats_by_store = {}
    for row in rows:
        store_name = str(row.get("store_name", "")).strip()
        if not store_name:
            continue
        stats_by_store[store_name] = {
            "store_name": store_name,
            "period_label": str(row.get("period_label", "")).strip(),
            "total_diff": _to_number(row.get("total_diff")),
            "avg_diff": _to_number(row.get("avg_diff")),
            "avg_games": _to_number(row.get("avg_games")),
            "win_rate": _to_number(row.get("win_rate")),
            "note": str(row.get("note", "")).strip(),
            "source": str(row.get("source", "")).strip(),
            "updated_at": str(row.get("updated_at", "")).strip(),
        }

    _cache_set("store_stats", stats_by_store)
    return stats_by_store


def save_store_stats(store_name, period_label="", total_diff=None, avg_diff=None,
                     avg_games=None, win_rate=None, note="", source=""):
    """
    店舗の年間データを保存する(1店舗1行。既に行があれば上書き更新)。
    戻り値: (成功したか, メッセージ)
    """
    store_name = (store_name or "").strip()
    if not store_name:
        return False, "店舗名が空のため保存できません。"

    values = [
        store_name,
        (period_label or "").strip(),
        "" if total_diff is None else total_diff,
        "" if avg_diff is None else avg_diff,
        "" if avg_games is None else avg_games,
        "" if win_rate is None else win_rate,
        (note or "").strip(),
        (source or "").strip(),
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    ]

    try:
        ws = get_store_stats_worksheet()
        existing = ws.col_values(1)  # store_name列(1行目はヘッダー)
        row_index = None
        for i, name in enumerate(existing[1:], start=2):
            if str(name).strip() == store_name:
                row_index = i
                break

        if row_index:
            _ensure_min_columns(ws, len(STORE_STATS_HEADERS))
            ws.update(f"A{row_index}:{chr(ord('A') + len(STORE_STATS_HEADERS) - 1)}{row_index}", [values])
            message = f"「{store_name}」の年間データを更新しました。"
        else:
            ws.append_row(values)
            message = f"「{store_name}」の年間データを登録しました。"
    except Exception as e:
        logger.error(f"店舗年間データの保存エラー: {e}")
        return False, "年間データの保存に失敗しました。時間をおいてお試しください。"

    _cache_invalidate("store_stats")
    return True, message


def describe_store_stats(stats):
    """店舗の年間データを、AIプロンプト用の1行テキストにする。"""
    if not stats:
        return "登録なし"

    parts = []
    period = stats.get("period_label") or "期間の記載なし"
    parts.append(f"対象期間: {period}")
    if stats.get("total_diff") is not None:
        parts.append(f"総差枚: {stats['total_diff']:+,.0f}枚")
    if stats.get("avg_diff") is not None:
        parts.append(f"平均差枚: {stats['avg_diff']:+,.0f}枚")
    if stats.get("avg_games") is not None:
        parts.append(f"平均G数: {stats['avg_games']:,.0f}G")
    if stats.get("win_rate") is not None:
        parts.append(f"勝率: {stats['win_rate']:.1f}%")
    if stats.get("note"):
        parts.append(f"備考: {stats['note']}")
    return " / ".join(parts)


def analyze_store_stats_image_with_gemini(base64_image, mime_type="image/jpeg"):
    """
    データサイト等の「店舗全体の集計データ」のスクリーンショットを解析し、
    総差枚・平均差枚・平均G数・勝率を抽出する。

    読み取れなかった項目は null のまま返す(推測で埋めさせない)。
    戻り値: 解析結果の辞書 / 失敗時は None
    """
    prompt = """
    パチスロ・パチンコのデータサイトなどで表示される「店舗全体の集計データ」の画像です。
    画像から読み取れる数値を、以下のJSON形式でのみ出力してください。他の文章は不要です。
    画像に写っていない項目は、推測で埋めず必ず null にしてください。

    {
      "store_name": "画像から読み取れる店舗名(読み取れなければ空文字)",
      "period_label": "集計期間の表記(例: 2025年, 直近1年, 2025/01-2025/12。読み取れなければ空文字)",
      "total_diff": 総差枚(枚単位の整数。プラスなら正、マイナスなら負。読み取れなければ null),
      "avg_diff": 平均差枚(枚単位の数値。読み取れなければ null),
      "avg_games": 平均ゲーム数(G単位の数値。読み取れなければ null),
      "win_rate": 勝率(パーセントの数値。例: 42.5。読み取れなければ null),
      "note": "上記以外に読み取れた補足(機種数・台数・営業日数など)があれば50文字程度で(日本語)"
    }

    数値はカンマ・「枚」「G」「%」などの単位を取り除いた数値のみにしてください。
    「平均差枚」が台あたりの平均なのか1日あたりなのか等の但し書きがあれば note に含めてください。
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
        logger.error("Gemini API タイムアウト(店舗年間データ解析)")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"Gemini API 通信エラー(店舗年間データ解析): {e}")
        return None

    raw_text = ""
    try:
        raw_text = response.json()["candidates"][0]["content"]["parts"][0]["text"]
        json_start = raw_text.find("{")
        json_end = raw_text.rfind("}") + 1
        if json_start == -1 or json_end == 0:
            logger.error(f"JSONが見つかりません(店舗年間データ解析): {raw_text}")
            return None
        parsed = json.loads(raw_text[json_start:json_end])
    except (KeyError, IndexError) as e:
        logger.error(f"Geminiレスポンス構造エラー(店舗年間データ解析): {e}")
        return None
    except json.JSONDecodeError as e:
        logger.error(f"JSON解析エラー(店舗年間データ解析): {e} / raw={raw_text!r}")
        return None

    return {
        "store_name": str(parsed.get("store_name", "") or "").strip(),
        "period_label": str(parsed.get("period_label", "") or "").strip(),
        "total_diff": _to_number(parsed.get("total_diff")),
        "avg_diff": _to_number(parsed.get("avg_diff")),
        "avg_games": _to_number(parsed.get("avg_games")),
        "win_rate": _to_number(parsed.get("win_rate")),
        "note": str(parsed.get("note", "") or "").strip(),
    }


# ---------------------------------------------------------------------------
# ホールデータ(店舗の日別データ)の貼り付け取り込み
# ---------------------------------------------------------------------------
# ホールデータサイトは自動アクセスをブロックしている(Cloudflareのボット判定)ため、
# サーバー側から直接取得することはできない。代わりに、ユーザーが自分のブラウザで
# 開いた一覧表をコピーして貼り付け、それを解析して一括登録する。
#
# 想定する貼り付け形式(1日1レコード。タブ区切りでも1セル1行でも解析できる):
#   2026/07/22(水)  +12,271  +11  5,053  42.2%(466/1104)
#   2026/07/21(火)  –        –    3,805  –
# 列の順番は「日付 / 総差枚 / 平均差枚 / 平均G数 / 勝率」を前提にしている。

# 日付トークン(末尾の曜日カッコは任意)
_HALL_DATE_RE = re.compile(
    r"^(\d{4})[/\-.年](\d{1,2})[/\-.月](\d{1,2})日?(?:[（(][^)）]*[)）])?$"
)
# 勝率トークン(例: "42.2%(466/1104)")
_HALL_WIN_RATE_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s*%(?:\s*[（(](\d+)\s*/\s*(\d+)[)）])?$")
# 「データなし」を表す記号(ダッシュ各種)
_HALL_EMPTY_TOKENS = {"-", "–", "—", "―", "ー", "‐", "−", "･", "・", "?", "？", "N/A", "n/a"}

# 1行として受け付ける値の上限(想定外の並びを取り込まないための保険)
_HALL_MAX_VALUES_PER_ROW = 8


def _is_empty_token(token):
    return token.strip() in _HALL_EMPTY_TOKENS or not token.strip()


def parse_hall_daily_text(text, max_rows=2000):
    """
    ホールデータ一覧をコピーしたテキストを解析し、日別データの行に変換する。

    戻り値: (rows, report)
      rows: [{"date": "YYYY-MM-DD", "total_diff": float|None, "avg_diff": float|None,
              "avg_games": float|None, "win_rate": float|None,
              "win_units": int|None, "total_units": int|None}, ...] 日付の新しい順
      report: {"parsed": 取り込める行数, "skipped": 列が足りず読み飛ばした行数,
               "skipped_samples": [読み飛ばした行の例], "with_diff": 差枚が入っている行数,
               "first_date", "last_date", "duplicates": 同じ日付が重複していた数}
    """
    report = {"parsed": 0, "skipped": 0, "skipped_samples": [], "with_diff": 0,
              "first_date": "", "last_date": "", "duplicates": 0}
    if not text or not text.strip():
        return [], report

    # タブ・空白のどちらで区切られていても、また1セル1行で貼られていても扱えるように、
    # いったん全部を「トークンの列」にほどいてから、日付トークンを区切りとして組み直す
    tokens = []
    for line in text.splitlines():
        for token in re.split(r"[\t　 ]+", line.strip()):
            if token:
                tokens.append(token)

    groups = []  # [(日付文字列, [値トークン...])]
    for token in tokens:
        matched = _HALL_DATE_RE.match(token)
        if matched:
            year, month, day = (int(g) for g in matched.groups())
            try:
                date_str = datetime(year, month, day).strftime("%Y-%m-%d")
            except ValueError:
                continue  # 2026/02/31 のような不正な日付は無視する
            groups.append((date_str, []))
        elif groups and len(groups[-1][1]) < _HALL_MAX_VALUES_PER_ROW:
            groups[-1][1].append(token)

    rows_by_date = {}
    for date_str, values in groups:
        win_rate = win_units = total_units = None
        numeric_tokens = []
        for value in values:
            win_matched = _HALL_WIN_RATE_RE.match(value)
            if win_matched:
                win_rate = float(win_matched.group(1))
                if win_matched.group(2) and win_matched.group(3):
                    win_units = int(win_matched.group(2))
                    total_units = int(win_matched.group(3))
            else:
                numeric_tokens.append(value)

        # 「総差枚 / 平均差枚 / 平均G数」の3つが揃っていない行は、
        # どの値がどの列なのかを推測することになるため、取り込まずに報告する
        if len(numeric_tokens) < 3:
            report["skipped"] += 1
            if len(report["skipped_samples"]) < 5:
                report["skipped_samples"].append(f"{date_str} {' '.join(values)}".strip())
            continue

        def _value(token):
            return None if _is_empty_token(token) else parse_number(token)

        row = {
            "date": date_str,
            "total_diff": _value(numeric_tokens[0]),
            "avg_diff": _value(numeric_tokens[1]),
            "avg_games": _value(numeric_tokens[2]),
            "win_rate": win_rate,
            "win_units": win_units,
            "total_units": total_units,
        }
        # 明らかに桁がおかしい値は取り込まない(貼り付けミス・列ズレの検出)
        if row["avg_games"] is not None and not (0 <= row["avg_games"] <= 100000):
            row["avg_games"] = None
        if row["win_rate"] is not None and not (0 <= row["win_rate"] <= 100):
            row["win_rate"] = None

        if not any(row[k] is not None for k in ("total_diff", "avg_diff", "avg_games", "win_rate")):
            report["skipped"] += 1
            if len(report["skipped_samples"]) < 5:
                report["skipped_samples"].append(f"{date_str} (数値なし)")
            continue

        if date_str in rows_by_date:
            report["duplicates"] += 1
        rows_by_date[date_str] = row
        if len(rows_by_date) >= max_rows:
            break

    rows = sorted(rows_by_date.values(), key=lambda r: r["date"], reverse=True)
    report["parsed"] = len(rows)
    report["with_diff"] = sum(1 for r in rows if r["total_diff"] is not None or r["avg_diff"] is not None)
    if rows:
        report["first_date"] = rows[-1]["date"]
        report["last_date"] = rows[0]["date"]
    return rows, report


def load_store_daily(store_name=""):
    """
    store_daily シートを読み込む。store_name を指定するとその店舗の行だけを返す。
    日付の新しい順。読み込みに失敗した場合は空リスト。
    """
    cached = _cache_get("store_daily")
    if cached is None:
        try:
            ws = get_store_daily_worksheet()
            raw_rows = ws.get_all_records()
        except Exception as e:
            logger.error(f"店舗日別データの読み込みエラー: {e}")
            return []

        cached = []
        for row in raw_rows:
            name = str(row.get("store_name", "")).strip()
            date_str = str(row.get("date", "")).strip()
            if not name or not date_str:
                continue
            cached.append({
                "store_name": name,
                "date": date_str,
                "total_diff": _to_number(row.get("total_diff")),
                "avg_diff": _to_number(row.get("avg_diff")),
                "avg_games": _to_number(row.get("avg_games")),
                "win_rate": _to_number(row.get("win_rate")),
                "win_units": _to_number(row.get("win_units")),
                "total_units": _to_number(row.get("total_units")),
                "source": str(row.get("source", "")).strip(),
            })
        cached.sort(key=lambda r: r["date"], reverse=True)
        _cache_set("store_daily", cached)

    if not store_name:
        return cached
    return [r for r in cached if r["store_name"] == store_name]


def save_store_daily_rows(store_name, rows, source="貼り付け取り込み"):
    """
    日別データをまとめて保存する(同じ店舗×同じ日付は上書き)。

    1回の取り込みで1000行以上になることがあるため、1行ずつ書かずに
    「既存データ + 今回分」をマージしてシート全体を1回で書き換える。
    戻り値: (成功したか, メッセージ, {"added": 新規, "updated": 上書き})
    """
    store_name = (store_name or "").strip()
    if not store_name:
        return False, "店舗名が空のため保存できません。", {}
    if not rows:
        return False, "取り込める行がありませんでした。", {}

    try:
        ws = get_store_daily_worksheet()
        existing = ws.get_all_records()
    except Exception as e:
        logger.error(f"店舗日別データの読み込みエラー(保存前): {e}")
        return False, "シートの読み込みに失敗しました。時間をおいてお試しください。", {}

    merged = {}
    for row in existing:
        name = str(row.get("store_name", "")).strip()
        date_str = str(row.get("date", "")).strip()
        if not name or not date_str:
            continue
        merged[(name, date_str)] = [
            name, date_str,
            row.get("total_diff", ""), row.get("avg_diff", ""), row.get("avg_games", ""),
            row.get("win_rate", ""), row.get("win_units", ""), row.get("total_units", ""),
            str(row.get("source", "")), str(row.get("updated_at", "")),
        ]

    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    added = updated = 0
    for row in rows:
        key = (store_name, row["date"])
        if key in merged:
            updated += 1
        else:
            added += 1
        merged[key] = [
            store_name, row["date"],
            "" if row.get("total_diff") is None else row["total_diff"],
            "" if row.get("avg_diff") is None else row["avg_diff"],
            "" if row.get("avg_games") is None else row["avg_games"],
            "" if row.get("win_rate") is None else row["win_rate"],
            "" if row.get("win_units") is None else row["win_units"],
            "" if row.get("total_units") is None else row["total_units"],
            source, now_text,
        ]

    values = [list(STORE_DAILY_HEADERS)]
    for key in sorted(merged, key=lambda k: (k[0], k[1])):
        values.append(merged[key])

    try:
        # 行数が足りないと書き込めないため、先にシートを必要な大きさへ広げる
        ws.resize(rows=max(len(values), 2), cols=len(STORE_DAILY_HEADERS))
        ws.update(values, "A1")
    except Exception as e:
        logger.error(f"店舗日別データの保存エラー: {e}")
        return False, "日別データの保存に失敗しました。時間をおいてお試しください。", {}

    _cache_invalidate("store_daily")
    return True, f"「{store_name}」の日別データを{added + updated}日分保存しました(新規{added}日 / 上書き{updated}日)。", {
        "added": added, "updated": updated,
    }


def _summarize_daily_rows(rows, overall_avg_games=None):
    """
    日別データのかたまりから、平均G数・平均差枚・勝率の平均を出す。
    項目ごとに「値が入っている日数」が違う(差枚は空欄の日が多い)ため、
    それぞれの有効日数も併せて返し、画面で母数が分かるようにする。
    """
    if not rows:
        return None

    def _mean(key):
        values = [r[key] for r in rows if r.get(key) is not None]
        return (sum(values) / len(values), len(values)) if values else (None, 0)

    avg_games, games_days = _mean("avg_games")
    avg_diff, diff_days = _mean("avg_diff")
    win_rate, win_days = _mean("win_rate")
    total_diffs = [r["total_diff"] for r in rows if r.get("total_diff") is not None]

    return {
        "count": len(rows),
        "avg_games": avg_games,
        "games_days": games_days,
        "avg_diff": avg_diff,
        "diff_days": diff_days,
        "win_rate": win_rate,
        "win_days": win_days,
        "total_diff_sum": sum(total_diffs) if total_diffs else None,
        # 全体平均に対する稼働の高さ(100が平均)。イベント日らしさの目安になる。
        "games_index": (avg_games / overall_avg_games * 100) if (avg_games and overall_avg_games) else None,
        "enough_samples": len(rows) >= TREND_MIN_SAMPLES,
    }


# ---------------------------------------------------------------------------
# 旧イベント日・周年日
# ---------------------------------------------------------------------------
# ホールデータサイトに載っている表記をそのまま貼れるように、書き方を何通りか受け付ける。
#   ゾロ目日 / 月日がゾロ目日 / 1のつく日 / 毎月7日 / 9月9日
_EVENT_ZOROME_RE = re.compile(r"^ゾロ目日?$")
_EVENT_MD_ZOROME_RE = re.compile(r"^月日(が)?ゾロ目日?$")
_EVENT_DIGIT_RE = re.compile(r"^(\d)の[付つ]く日$")
_EVENT_MONTH_DAY_RE = re.compile(r"^(\d{1,2})月(\d{1,2})日$")
_EVENT_DAY_RE = re.compile(r"^(?:毎月)?(\d{1,2})日$")
# ゾロ目日として扱う日(31日は「3と1」でゾロ目にならないため入れない)
_ZOROME_DAYS = (11, 22)


def parse_event_day_rules(text):
    """
    旧イベント日・周年日の指定テキストを、判定用のルールに変換する。

    戻り値: (rules, unknown)
      rules: [{"type", "value", "label"}, ...]
      unknown: 解釈できなかった語(画面で知らせて書き直してもらうため)
    """
    if not text or not str(text).strip():
        return [], []

    # 「ゾロ目日(11日、22日)」のようにカッコ内に具体日が書かれている表記に合わせ、
    # カッコも区切り文字として扱う
    normalized = re.sub(r"[（）()]", "、", str(text))
    tokens = [t.strip() for t in re.split(r"[、,／/\n\r\t]+", normalized) if t.strip()]

    rules = []
    unknown = []
    seen = set()

    def _add(rule_type, value, label):
        key = (rule_type, value)
        if key in seen:
            return
        seen.add(key)
        rules.append({"type": rule_type, "value": value, "label": label})

    for token in tokens:
        cleaned = token.replace(" ", "").replace("　", "")
        if _EVENT_ZOROME_RE.match(cleaned):
            _add("zorome", None, "ゾロ目日")
            continue
        if _EVENT_MD_ZOROME_RE.match(cleaned):
            _add("md_zorome", None, "月日ゾロ目")
            continue
        matched = _EVENT_DIGIT_RE.match(cleaned)
        if matched:
            digit = int(matched.group(1))
            _add("digit", digit, f"{digit}のつく日")
            continue
        matched = _EVENT_MONTH_DAY_RE.match(cleaned)
        if matched:
            month, day = int(matched.group(1)), int(matched.group(2))
            if 1 <= month <= 12 and 1 <= day <= 31:
                _add("date", (month, day), f"{month}月{day}日")
                continue
        matched = _EVENT_DAY_RE.match(cleaned)
        if matched:
            day = int(matched.group(1))
            if 1 <= day <= 31:
                _add("day", day, f"毎月{day}日")
                continue
        unknown.append(token)

    return rules, unknown


def _rule_matches_date(rule, day):
    """1つのルールがその日付に当てはまるか"""
    rule_type = rule["type"]
    if rule_type == "zorome":
        return day.day in _ZOROME_DAYS
    if rule_type == "md_zorome":
        return day.month == day.day
    if rule_type == "digit":
        return str(rule["value"]) in str(day.day)
    if rule_type == "day":
        return day.day == rule["value"]
    if rule_type == "date":
        return (day.month, day.day) == tuple(rule["value"])
    return False


def matched_event_labels(day, rules):
    """その日に当てはまるルールのラベルを返す"""
    return [r["label"] for r in rules if _rule_matches_date(r, day)]


def describe_event_rules(rules):
    """ルールを画面表示用の文字列にする"""
    return "、".join(r["label"] for r in rules) if rules else ""


def load_store_events():
    """
    store_events シートを読み込み、{店舗名: 設定} の辞書で返す。
    読めない場合は空辞書(この機能が使えないだけで他は今まで通り動かす)。
    """
    cached = _cache_get("store_events")
    if cached is not None:
        return cached

    try:
        ws = get_store_events_worksheet()
        rows = ws.get_all_records()
    except Exception as e:
        logger.error(f"店舗イベント日の読み込みエラー: {e}")
        return {}

    events_by_store = {}
    for row in rows:
        store_name = str(row.get("store_name", "")).strip()
        if not store_name:
            continue
        event_text = str(row.get("event_days", "")).strip()
        anniversary_text = str(row.get("anniversary_days", "")).strip()
        event_rules, event_unknown = parse_event_day_rules(event_text)
        anniversary_rules, anniversary_unknown = parse_event_day_rules(anniversary_text)
        events_by_store[store_name] = {
            "store_name": store_name,
            "event_days": event_text,
            "anniversary_days": anniversary_text,
            "event_rules": event_rules,
            "anniversary_rules": anniversary_rules,
            "unknown": event_unknown + anniversary_unknown,
            "note": str(row.get("note", "")).strip(),
            "source": str(row.get("source", "")).strip(),
            "updated_at": str(row.get("updated_at", "")).strip(),
        }

    _cache_set("store_events", events_by_store)
    return events_by_store


def save_store_events(store_name, event_days="", anniversary_days="", note="", source=""):
    """
    店舗の旧イベント日・周年日を保存する(1店舗1行。既に行があれば上書き)。
    戻り値: (成功したか, メッセージ)
    """
    store_name = (store_name or "").strip()
    if not store_name:
        return False, "店舗名が空のため保存できません。"

    event_rules, event_unknown = parse_event_day_rules(event_days)
    anniversary_rules, anniversary_unknown = parse_event_day_rules(anniversary_days)
    unknown = event_unknown + anniversary_unknown

    try:
        ws = get_store_events_worksheet()
        existing = ws.get_all_records()
    except Exception as e:
        logger.error(f"店舗イベント日の読み込みエラー(保存前): {e}")
        return False, "シートの読み込みに失敗しました。時間をおいてお試しください。"

    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    values = [store_name, event_days.strip(), anniversary_days.strip(),
              note.strip(), source, now_text]

    target_row = None
    for index, row in enumerate(existing, start=2):
        if str(row.get("store_name", "")).strip() == store_name:
            target_row = index
            break

    try:
        if target_row:
            ws.update([values], f"A{target_row}")
        else:
            ws.append_row(values)
    except Exception as e:
        logger.error(f"店舗イベント日の保存エラー: {e}")
        return False, "イベント日の保存に失敗しました。時間をおいてお試しください。"

    _cache_invalidate("store_events")
    message = f"「{store_name}」の旧イベント日・周年日を保存しました。"
    if event_rules or anniversary_rules:
        message += f"(旧イベ: {describe_event_rules(event_rules) or 'なし'} / 周年: {describe_event_rules(anniversary_rules) or 'なし'})"
    if unknown:
        message += f" 読み取れなかった指定: {'、'.join(unknown)}"
    return True, message


_EVENT_CATEGORY_LABELS = {"event": "旧イベント日", "anniversary": "周年日", "normal": "通常日"}


def _event_category_rows(parsed_dates, events, overall):
    """旧イベント日 / 周年日 / 通常日 の3つに分けて集計する(日は重複して属することがある)"""
    buckets = {"event": [], "anniversary": [], "normal": []}
    for day, row in parsed_dates:
        is_event = any(_rule_matches_date(r, day) for r in events["event_rules"])
        is_anniversary = any(_rule_matches_date(r, day) for r in events["anniversary_rules"])
        if is_event:
            buckets["event"].append(row)
        if is_anniversary:
            buckets["anniversary"].append(row)
        if not is_event and not is_anniversary:
            buckets["normal"].append(row)

    rows = []
    for key in ("event", "anniversary", "normal"):
        summary = _summarize_daily_rows(buckets[key], overall_avg_games=overall["avg_games"])
        if summary is None:
            continue
        summary["key"] = key
        summary["label"] = _EVENT_CATEGORY_LABELS[key]
        rows.append(summary)
    return rows


def _event_rule_rows(parsed_dates, events, overall):
    """設定したルールごとの集計(「11日だけ強い」のような差を見るため)"""
    rows = []
    seen = set()
    for rule in list(events["event_rules"]) + list(events["anniversary_rules"]):
        if rule["label"] in seen:
            continue
        seen.add(rule["label"])
        group = [row for day, row in parsed_dates if _rule_matches_date(rule, day)]
        summary = _summarize_daily_rows(group, overall_avg_games=overall["avg_games"])
        if summary is None:
            continue
        summary["key"] = rule["label"]
        summary["label"] = rule["label"]
        rows.append(summary)
    # 稼働の高い順。G数が無い行は後ろに送る
    rows.sort(key=lambda r: (r["avg_games"] is None, -(r["avg_games"] or 0)))
    return rows


def build_store_daily_trends(store_name, days=365):
    """
    取り込んだ店舗の日別データを、曜日別・日付末尾別に集計する。

    自分の記録(build_store_trends)と違い、こちらは店の全台が対象の数字。
    ただし差枚・勝率はサイト側で空欄の日が多いため、稼働(平均G数)を主軸に、
    値がある日だけで平均を出す。
    """
    store_name = (store_name or "").strip()
    if not store_name:
        return None

    rows = load_store_daily(store_name)
    if days > 0:
        limit_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = [r for r in rows if r["date"] >= limit_date]

    trends = {"store_name": store_name, "days": days, "record_count": len(rows),
              "min_samples": TREND_MIN_SAMPLES}
    if not rows:
        trends["overall"] = None
        return trends

    overall = _summarize_daily_rows(rows)
    trends["overall"] = overall
    trends["first_date"] = rows[-1]["date"]
    trends["last_date"] = rows[0]["date"]
    trends["recent"] = rows[:30]

    parsed_dates = []
    for r in rows:
        try:
            parsed_dates.append((datetime.strptime(r["date"], "%Y-%m-%d"), r))
        except ValueError:
            continue

    def _group(key_func, label_func, sort_key):
        buckets = {}
        for dt, r in parsed_dates:
            buckets.setdefault(key_func(dt), []).append(r)
        result = []
        for key, group_rows in buckets.items():
            summary = _summarize_daily_rows(group_rows, overall_avg_games=overall["avg_games"])
            summary["key"] = key
            summary["label"] = label_func(key)
            result.append(summary)
        result.sort(key=sort_key)
        return result

    trends["by_weekday"] = _group(lambda dt: dt.weekday(), lambda k: f"{WEEKDAY_LABELS[k]}曜", lambda row: row["key"])
    trends["by_day_suffix"] = _group(lambda dt: dt.day % 10, lambda k: f"末尾{k}の日", lambda row: row["key"])

    # 旧イベント日・周年日が登録されていれば、その日と通常日を比べられるようにする
    events = load_store_events().get(store_name)
    trends["events"] = events
    trends["by_event_category"] = []
    trends["by_event_rule"] = []
    if events and (events["event_rules"] or events["anniversary_rules"]):
        trends["by_event_category"] = _event_category_rows(parsed_dates, events, overall)
        trends["by_event_rule"] = _event_rule_rows(parsed_dates, events, overall)

    def _best(rows_, key):
        eligible = [r for r in rows_ if r["enough_samples"] and r.get(key) is not None]
        return max(eligible, key=lambda r: r[key]) if eligible else None

    trends["highlights"] = {
        "busiest_weekday": _best(trends["by_weekday"], "avg_games"),
        "busiest_day_suffix": _best(trends["by_day_suffix"], "avg_games"),
        "best_diff_day_suffix": _best(trends["by_day_suffix"], "avg_diff"),
        "busiest_event_rule": _best(trends["by_event_rule"], "avg_games"),
    }

    # 「旧イベ日は通常日よりどれだけ動くか」は一番知りたい所なので、差を先に出しておく
    by_key = {row["key"]: row for row in trends["by_event_category"]}
    event_row, normal_row = by_key.get("event"), by_key.get("normal")
    if event_row and normal_row and event_row["avg_games"] and normal_row["avg_games"]:
        trends["event_vs_normal"] = {
            "event": event_row,
            "normal": normal_row,
            "games_ratio": event_row["avg_games"] / normal_row["avg_games"] * 100,
            "games_gap": event_row["avg_games"] - normal_row["avg_games"],
            "diff_gap": (event_row["avg_diff"] - normal_row["avg_diff"])
                        if (event_row["avg_diff"] is not None and normal_row["avg_diff"] is not None) else None,
        }
    else:
        trends["event_vs_normal"] = None
    return trends


def describe_store_daily_trends(trends, limit=10):
    """取り込んだ日別データの集計を、AIプロンプト用のテキストにまとめる。"""
    if not trends or not trends.get("record_count"):
        return "登録なし"

    overall = trends["overall"]
    lines = [
        f"対象: {trends.get('first_date', '')}〜{trends.get('last_date', '')} の{trends['record_count']}日分"
        f"(店の全台が対象の外部データ)",
        f"全体: 平均G数{overall['avg_games']:.0f}G({overall['games_days']}日分)"
        + (f", 平均差枚{overall['avg_diff']:+.0f}枚({overall['diff_days']}日分)" if overall["avg_diff"] is not None else ", 平均差枚はデータなし")
        + (f", 勝率{overall['win_rate']:.1f}%({overall['win_days']}日分)" if overall["win_rate"] is not None else ""),
    ]

    def _rows_text(title, rows):
        if not rows:
            return f"{title}: データなし"
        parts = []
        for row in rows[:limit]:
            piece = f"{row['label']}: 平均G数{row['avg_games']:.0f}G/{row['count']}日" if row["avg_games"] is not None else f"{row['label']}: G数データなし/{row['count']}日"
            if row.get("games_index") is not None:
                piece += f"(全体比{row['games_index']:.0f}%)"
            if row.get("avg_diff") is not None:
                piece += f", 平均差枚{row['avg_diff']:+.0f}枚({row['diff_days']}日)"
            parts.append(piece)
        return f"{title}: " + " / ".join(parts)

    lines.append(_rows_text("曜日別", trends.get("by_weekday", [])))
    lines.append(_rows_text("日付末尾別", trends.get("by_day_suffix", [])))

    if trends.get("by_event_category"):
        lines.append(_rows_text("旧イベント日/周年日/通常日", trends["by_event_category"]))
    if trends.get("by_event_rule"):
        lines.append(_rows_text("イベント日の種類別", trends["by_event_rule"]))
    comparison = trends.get("event_vs_normal")
    if comparison:
        piece = (f"旧イベント日の稼働は通常日の{comparison['games_ratio']:.0f}%"
                 f"({comparison['games_gap']:+.0f}G)")
        if comparison["diff_gap"] is not None:
            piece += f", 平均差枚の差は{comparison['diff_gap']:+.0f}枚"
        lines.append(piece)
    return "\n".join(lines)


def describe_store_day_context(store_name, when=None):
    """
    「今日はこの店にとってどういう日か」を、取り込んだホールデータから1行にまとめる。

    設定推測のプロンプトに添えて、曜日・日付末尾ごとの稼働の高さ(全体比)を
    ホールの傾向を測る材料として渡すために使う。
    データが無い場合は空文字を返す(プロンプトに余計な行を増やさない)。
    """
    trends = build_store_daily_trends(store_name, days=365)
    if not trends or not trends.get("record_count"):
        return ""

    when = when or datetime.now()
    weekday_row = next((r for r in trends.get("by_weekday", []) if r["key"] == when.weekday()), None)
    suffix_row = next((r for r in trends.get("by_day_suffix", []) if r["key"] == when.day % 10), None)

    parts = []
    if weekday_row and weekday_row.get("games_index") is not None:
        parts.append(f"{weekday_row['label']}の稼働は店全体平均の{weekday_row['games_index']:.0f}%"
                     f"(平均{weekday_row['avg_games']:.0f}G / {weekday_row['count']}日分)")
    if suffix_row and suffix_row.get("games_index") is not None:
        parts.append(f"{suffix_row['label']}の稼働は{suffix_row['games_index']:.0f}%"
                     f"(平均{suffix_row['avg_games']:.0f}G / {suffix_row['count']}日分)")
    if not parts:
        return ""

    return (f"{when.strftime('%Y-%m-%d')}時点: " + " / ".join(parts)
            + "(稼働が高い日はイベント等で設定を使っている可能性があるが、稼働だけでは設定は分からない)")


# ---------------------------------------------------------------------------
# 台別データ(機種・台番号ごと)の貼り付け取り込み
# ---------------------------------------------------------------------------
# 日別詳細ページには「機種名の見出し + 台番号ごとの行」が並んでいる。
# サイトによって列の並びが違うため、見出し行があればそこから列の対応を読み取り、
# 見出しが無い場合だけ既定の並び(台番号/G数/差枚/BB/RB)を使う。

# 見出し行から列を特定するための表記ゆれ一覧(先に書いたものが優先)
_UNIT_COLUMN_ALIASES = [
    ("machine_number", ("台番号", "台番", "台No", "台no", "番号", "台")),
    ("total_games", ("総回転数", "総G数", "ゲーム数", "回転数", "G数", "G")),
    ("difference_slabs", ("差枚数", "差枚", "差玉", "出玉")),
    ("big_count", ("BB回数", "BIG回数", "BB", "BIG", "ビッグ")),
    ("reg_count", ("RB回数", "REG回数", "RB", "REG", "レギュラー")),
]
# 見出しが無いときに使う既定の並び
_UNIT_DEFAULT_COLUMNS = ["machine_number", "total_games", "difference_slabs", "big_count", "reg_count"]
# 画面に「どの列をどう読んだか」を出すための表示名
_UNIT_FIELD_LABELS = {
    "machine_number": "台番号", "total_games": "G数", "difference_slabs": "差枚",
    "big_count": "BB", "reg_count": "RB",
}

# 機種名の見出しとして扱わない行(サイトの共通パーツや集計行)
_UNIT_NOISE_WORDS = (
    "スポンサーリンク", "広告", "合計", "平均", "総台数", "検索", "ホーム", "HOME",
    "ページ", "ランキング", "詳細", "戻る", "データ一覧", "前日", "翌日",
)
_UNIT_MAX_MACHINE_NAME_LEN = 40


def _split_row_tokens(line):
    """1行をセルのリストに分ける(タブ優先。タブが無ければ2個以上の空白か単一空白で分ける)"""
    if "\t" in line:
        return [t.strip() for t in line.split("\t") if t.strip()]
    if re.search(r"[ 　]{2,}", line):
        return [t.strip() for t in re.split(r"[ 　]{2,}", line) if t.strip()]
    return [t.strip() for t in re.split(r"[ 　]+", line) if t.strip()]


def _match_unit_header(tokens):
    """
    見出し行なら列名のリストを返す(例: ["machine_number","total_games",...])。
    見出しでなければ None。
    """
    if len(tokens) < 2:
        return None
    columns = []
    matched = 0
    for token in tokens:
        cleaned = token.replace(" ", "").replace("　", "")
        found = None
        for field, aliases in _UNIT_COLUMN_ALIASES:
            if any(alias == cleaned for alias in aliases):
                found = field
                break
        if found is None:
            for field, aliases in _UNIT_COLUMN_ALIASES:
                if any(alias in cleaned for alias in aliases):
                    found = field
                    break
        columns.append(found)
        if found:
            matched += 1
    # 見出し行とみなす条件を厳しめにしている。
    # 列名の判定は部分一致も見ているため("G数"に対する"G"など)、
    # 英字を含む機種名("ビッグドリーム THE GOLDEN PUSHER" など)が
    # 見出し行として誤検出され、以降の列の対応が全部ずれてしまうのを防ぐ。
    # 実際の見出し行はほぼ全てのセルが既知の列名になり、台番号の列を必ず含む。
    if matched >= 3 and matched >= len(tokens) * 0.75 and "machine_number" in columns:
        return columns
    return None


def _looks_like_unit_row(tokens):
    """台番号ではじまり、数値が2つ以上並ぶ行かどうか"""
    if len(tokens) < 3:
        return False
    first = tokens[0].replace(",", "").replace("番", "")
    if not first.isdigit() or not (0 < int(first) <= 9999):
        return False
    numeric_count = sum(1 for t in tokens[1:] if parse_number(t) is not None)
    return numeric_count >= 2


def parse_hall_unit_text(text, max_rows=3000):
    """
    日別詳細ページ(機種・台番号ごとのデータ)をコピーしたテキストを解析する。

    戻り値: (rows, report)
      rows: [{"machine_name", "machine_number", "total_games", "difference_slabs",
              "big_count", "reg_count"}, ...] 台番号順
      report: {"parsed", "skipped", "skipped_samples", "machines", "columns",
               "header_found", "with_diff"}
    """
    def _column_labels(columns):
        return [_UNIT_FIELD_LABELS.get(c, "使わない列") for c in columns]

    report = {"parsed": 0, "skipped": 0, "skipped_samples": [], "machines": [],
              "columns": _column_labels(_UNIT_DEFAULT_COLUMNS), "header_found": False, "with_diff": 0}
    if not text or not text.strip():
        return [], report

    columns = list(_UNIT_DEFAULT_COLUMNS)
    current_machine = ""
    machines = []
    rows_by_number = {}

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        tokens = _split_row_tokens(line)
        if not tokens:
            continue

        header = _match_unit_header(tokens)
        if header:
            columns = header
            report["header_found"] = True
            report["columns"] = _column_labels(header)
            continue

        if _looks_like_unit_row(tokens):
            row = {"machine_name": current_machine, "machine_number": "",
                   "total_games": None, "difference_slabs": None,
                   "big_count": None, "reg_count": None}
            for index, token in enumerate(tokens):
                field = columns[index] if index < len(columns) else None
                if not field or field == "-":
                    continue
                if field == "machine_number":
                    row["machine_number"] = token.replace(",", "").replace("番", "").strip()
                else:
                    row[field] = parse_number(token)
            if not row["machine_number"]:
                row["machine_number"] = tokens[0].replace(",", "").strip()
            # G数も差枚も読めない行は、列がズレている可能性が高いので取り込まない
            if row["total_games"] is None and row["difference_slabs"] is None:
                report["skipped"] += 1
                if len(report["skipped_samples"]) < 5:
                    report["skipped_samples"].append(line[:60])
                continue
            rows_by_number[row["machine_number"]] = row
            if len(rows_by_number) >= max_rows:
                break
            continue

        # 数値行でも見出し行でもない短いテキストは、機種名の見出しとみなす
        if len(line) <= _UNIT_MAX_MACHINE_NAME_LEN and not any(w in line for w in _UNIT_NOISE_WORDS):
            if re.search(r"[^\d\s,.\-+%/–—]", line):
                current_machine = line
                if current_machine not in machines:
                    machines.append(current_machine)
                continue

        report["skipped"] += 1
        if len(report["skipped_samples"]) < 5:
            report["skipped_samples"].append(line[:60])

    rows = sorted(rows_by_number.values(),
                  key=lambda r: (int(r["machine_number"]) if r["machine_number"].isdigit() else 99999))
    report["parsed"] = len(rows)
    report["machines"] = machines
    report["with_diff"] = sum(1 for r in rows if r["difference_slabs"] is not None)
    return rows, report


# アナスロ取り込みCSV(tools/anaslo.py が出す形)の列名。表記ゆれも拾えるようにしている。
_ANASLO_CSV_ALIASES = {
    "date": "date", "日付": "date",
    "store_name": "store_name", "店舗名": "store_name", "店名": "store_name",
    "machine_number": "machine_number", "台番号": "machine_number", "台番": "machine_number",
    "machine_name": "machine_name", "機種名": "machine_name",
    "games": "total_games", "total_games": "total_games", "g数": "total_games", "回転数": "total_games",
    "diff": "difference_slabs", "difference_slabs": "difference_slabs", "差枚": "difference_slabs",
    "bb": "big_count", "big_count": "big_count",
    "rb": "reg_count", "reg_count": "reg_count",
    "art": "art_count", "art_count": "art_count",
}


def parse_anaslo_csv_text(text, max_rows=60000):
    """
    ホールデータ取り込みツール(tools/anaslo.py)が出すCSVを解析する。

    1行が「1日×1台」なので、これ1つから台別データと日別データの両方を作れる。
    日別の値(総差枚・平均差枚・平均G数・勝率)は台別データから計算する。

    戻り値: (units_by_date, daily_rows, report)
      units_by_date: {"YYYY-MM-DD": [台別データの行, ...]}
      daily_rows: 日別データの行(日付の新しい順)
    """
    report = {"units": 0, "dates": 0, "machines": 0, "skipped": 0, "skipped_samples": [],
              "first_date": "", "last_date": "", "with_art": 0, "store_names": []}
    if not text or not text.strip():
        return {}, [], report

    # utf-8-sig で読んだ場合に残るBOMと、CRLFの取り扱いを揃えてから csv に渡す
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    if not reader.fieldnames:
        return {}, [], report

    columns = {}
    for name in reader.fieldnames:
        key = (name or "").strip().lstrip("\ufeff").lower()
        if key in _ANASLO_CSV_ALIASES:
            columns[name] = _ANASLO_CSV_ALIASES[key]
    if "date" not in columns.values() or "machine_number" not in columns.values():
        return {}, [], report

    units_by_date = {}
    machines = set()
    store_names = set()

    for raw in reader:
        row = {}
        for name, field in columns.items():
            row[field] = (raw.get(name) or "").strip()

        date_str = _normalize_anaslo_date(row.get("date", ""))
        number = row.get("machine_number", "").replace(",", "")
        if not date_str or not number.isdigit():
            report["skipped"] += 1
            if len(report["skipped_samples"]) < 5:
                report["skipped_samples"].append(f"{row.get('date', '')} {row.get('machine_number', '')}".strip())
            continue

        unit = {
            "machine_name": row.get("machine_name", ""),
            "machine_number": number,
            "total_games": parse_number(row.get("total_games")),
            "difference_slabs": parse_number(row.get("difference_slabs")),
            "big_count": parse_number(row.get("big_count")),
            "reg_count": parse_number(row.get("reg_count")),
            "art_count": parse_number(row.get("art_count")),
        }
        # G数も差枚も無い行は、列がずれているか空行なので取り込まない
        if unit["total_games"] is None and unit["difference_slabs"] is None:
            report["skipped"] += 1
            if len(report["skipped_samples"]) < 5:
                report["skipped_samples"].append(f"{date_str} {number} (数値なし)")
            continue

        units_by_date.setdefault(date_str, {})[number] = unit
        if unit["machine_name"]:
            machines.add(unit["machine_name"])
        if unit["art_count"]:
            report["with_art"] += 1
        if row.get("store_name"):
            store_names.add(row["store_name"])
        if sum(len(v) for v in units_by_date.values()) >= max_rows:
            break

    # 同じ日×同じ台番号は後の行で上書きされるので、ここで並べ直す
    units_by_date = {
        date_str: sorted(units.values(), key=lambda u: int(u["machine_number"]))
        for date_str, units in units_by_date.items()
    }

    daily_rows = [_daily_row_from_units(date_str, units)
                  for date_str, units in units_by_date.items()]
    daily_rows.sort(key=lambda r: r["date"], reverse=True)

    dates = sorted(units_by_date)
    report["units"] = sum(len(v) for v in units_by_date.values())
    report["dates"] = len(dates)
    report["machines"] = len(machines)
    report["store_names"] = sorted(store_names)
    if dates:
        report["first_date"], report["last_date"] = dates[0], dates[-1]
    return units_by_date, daily_rows, report


def _normalize_anaslo_date(value):
    """"2026-08-22" や "2026/08/22(土)" を "YYYY-MM-DD" に揃える"""
    matched = re.search(r"(\d{4})[/\-.年](\d{1,2})[/\-.月](\d{1,2})", value or "")
    if not matched:
        return ""
    year, month, day = (int(g) for g in matched.groups())
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _daily_row_from_units(date_str, units):
    """
    その日の台別データから、店舗全体の日別データを作る。

    平均G数・平均差枚は全台の単純平均、勝率は差枚がプラスだった台の割合。
    これはホールデータサイトが一覧に出している値と同じ定義になっている。
    """
    diffs = [u["difference_slabs"] for u in units if u.get("difference_slabs") is not None]
    games = [u["total_games"] for u in units if u.get("total_games") is not None]
    wins = sum(1 for d in diffs if d > 0)
    return {
        "date": date_str,
        "total_diff": sum(diffs) if diffs else None,
        "avg_diff": round(sum(diffs) / len(diffs)) if diffs else None,
        "avg_games": round(sum(games) / len(games)) if games else None,
        "win_rate": round(100 * wins / len(diffs), 1) if diffs else None,
        "win_units": wins if diffs else None,
        "total_units": len(diffs) if diffs else None,
    }


def load_store_units(store_name=""):
    """store_units シートを読み込む(日付の新しい順)。読み込み失敗時は空リスト。"""
    cached = _cache_get("store_units")
    if cached is None:
        try:
            ws = get_store_units_worksheet()
            raw_rows = ws.get_all_records()
        except Exception as e:
            logger.error(f"台別データの読み込みエラー: {e}")
            return []

        cached = []
        for row in raw_rows:
            name = str(row.get("store_name", "")).strip()
            date_str = str(row.get("date", "")).strip()
            number = str(row.get("machine_number", "")).strip()
            if not name or not date_str or not number:
                continue
            cached.append({
                "store_name": name,
                "date": date_str,
                "machine_name": str(row.get("machine_name", "")).strip(),
                "machine_number": number,
                "total_games": _to_number(row.get("total_games")),
                "difference_slabs": _to_number(row.get("difference_slabs")),
                "big_count": _to_number(row.get("big_count")),
                "reg_count": _to_number(row.get("reg_count")),
            })
        cached.sort(key=lambda r: (r["date"], r["machine_number"]), reverse=True)
        _cache_set("store_units", cached)

    if not store_name:
        return cached
    return [r for r in cached if r["store_name"] == store_name]


def save_store_unit_rows(store_name, date_str, rows, source="貼り付け取り込み"):
    """台別データを1日分保存する(中身は複数日版と同じ処理)"""
    date_str = (date_str or "").strip()
    if not date_str:
        return False, "対象の日付を指定してください。", {}
    ok, message, counts = save_store_unit_rows_multi(store_name, {date_str: rows}, source=source)
    if ok:
        message = (f"「{store_name}」{date_str}の台別データを{counts['added'] + counts['updated']}台分"
                   f"保存しました(新規{counts['added']}台 / 上書き{counts['updated']}台)。")
    return ok, message, counts


def save_store_unit_rows_multi(store_name, rows_by_date, source="貼り付け取り込み"):
    """
    台別データを複数日ぶんまとめて保存する(同じ店舗×日付×台番号は上書き)。

    1日分で1000台を超えることがあり、日ごとにシートを書き換えると
    取り込む日数だけ全体の書き込みが走ってしまうため、何日分でも1回の書き込みにまとめる。
    戻り値: (成功したか, メッセージ, {"added", "updated", "dates"})
    """
    store_name = (store_name or "").strip()
    if not store_name:
        return False, "店舗名が空のため保存できません。", {}
    rows_by_date = {d: r for d, r in (rows_by_date or {}).items() if d and r}
    if not rows_by_date:
        return False, "取り込める行がありませんでした。", {}

    try:
        ws = get_store_units_worksheet()
        existing = ws.get_all_records()
    except Exception as e:
        logger.error(f"台別データの読み込みエラー(保存前): {e}")
        return False, "シートの読み込みに失敗しました。時間をおいてお試しください。", {}

    merged = {}
    for row in existing:
        key = (str(row.get("store_name", "")).strip(), str(row.get("date", "")).strip(),
               str(row.get("machine_number", "")).strip())
        if not all(key):
            continue
        merged[key] = [
            key[0], key[1], str(row.get("machine_name", "")), key[2],
            row.get("total_games", ""), row.get("difference_slabs", ""),
            row.get("big_count", ""), row.get("reg_count", ""),
            str(row.get("source", "")), str(row.get("updated_at", "")),
            row.get("art_count", ""),
        ]

    now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    added = updated = 0
    for date_str, rows in rows_by_date.items():
        for row in rows:
            key = (store_name, date_str, str(row["machine_number"]))
            if key in merged:
                updated += 1
            else:
                added += 1
            merged[key] = [
                store_name, date_str, row.get("machine_name", ""), str(row["machine_number"]),
                "" if row.get("total_games") is None else row["total_games"],
                "" if row.get("difference_slabs") is None else row["difference_slabs"],
                "" if row.get("big_count") is None else row["big_count"],
                "" if row.get("reg_count") is None else row["reg_count"],
                source, now_text,
                "" if row.get("art_count") is None else row["art_count"],
            ]

    values = [list(STORE_UNITS_HEADERS)]
    for key in sorted(merged, key=lambda k: (k[0], k[1], int(k[2]) if k[2].isdigit() else 99999)):
        values.append(merged[key])

    try:
        ws.resize(rows=max(len(values), 2), cols=len(STORE_UNITS_HEADERS))
        ws.update(values, "A1")
    except Exception as e:
        logger.error(f"台別データの保存エラー: {e}")
        return False, "台別データの保存に失敗しました。時間をおいてお試しください。", {}

    _cache_invalidate("store_units")
    dates = sorted(rows_by_date)
    return True, (f"「{store_name}」の台別データを{len(dates)}日分・{added + updated}台分"
                  f"保存しました(新規{added}台 / 上書き{updated}台)。"), {
        "added": added, "updated": updated, "dates": dates,
    }


def _summarize_unit_rows(rows):
    """
    台別データのかたまりを集計する。
    差枚が空欄の台があるため、平均差枚・勝率は「差枚が入っている台」だけで計算し、
    その母数(diff_count)も返す。表示・マクロは自分の記録の集計と同じ形に合わせてある。
    """
    if not rows:
        return None
    diffs = [r["difference_slabs"] for r in rows if r.get("difference_slabs") is not None]
    games = [r["total_games"] for r in rows if r.get("total_games") is not None]
    plus_count = sum(1 for d in diffs if d > 0)
    return {
        "count": len(rows),
        "diff_count": len(diffs),
        "avg_diff": (sum(diffs) / len(diffs)) if diffs else 0,
        "total_diff": sum(diffs) if diffs else 0,
        "best_diff": max(diffs) if diffs else 0,
        "worst_diff": min(diffs) if diffs else 0,
        "plus_count": plus_count,
        "plus_rate": (plus_count / len(diffs) * 100) if diffs else 0,
        "avg_games": (sum(games) / len(games)) if games else 0,
        "enough_samples": len(diffs) >= TREND_MIN_SAMPLES,
    }


def _mark_edge_units(rows):
    """
    「端台(角台の候補)」に印を付ける。

    角台かどうかは台番号だけでは確定できないため、同じ日・同じ機種の台番号を
    連番のかたまりに分け、その両端を端台とみなす近似で判定する。
    (島の切れ目と機種の切れ目が一致しない場合はズレるので、あくまで目安)
    3台以上のかたまりだけを対象にする(2台以下だと全部が端になってしまうため)。
    """
    groups = {}
    for row in rows:
        if not str(row.get("machine_number", "")).isdigit():
            row["is_edge"] = None
            continue
        groups.setdefault((row["date"], row.get("machine_name", "")), []).append(row)

    for group_rows in groups.values():
        group_rows.sort(key=lambda r: int(r["machine_number"]))
        run = [group_rows[0]]
        runs = [run]
        for previous, current in zip(group_rows, group_rows[1:]):
            if int(current["machine_number"]) - int(previous["machine_number"]) == 1:
                run.append(current)
            else:
                run = [current]
                runs.append(run)
        for one_run in runs:
            if len(one_run) < 3:
                for row in one_run:
                    row["is_edge"] = None  # 判定できるだけの並びが無い
                continue
            for index, row in enumerate(one_run):
                row["is_edge"] = index in (0, len(one_run) - 1)
    return rows


def build_store_unit_trends(store_name, days=90):
    """
    取り込んだ台別データから、台番号末尾・端台(角台の候補)・機種・台番号ごとの傾向を集計する。
    """
    store_name = (store_name or "").strip()
    if not store_name:
        return None

    rows = [dict(r) for r in load_store_units(store_name)]
    if days > 0:
        limit_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        rows = [r for r in rows if r["date"] >= limit_date]

    trends = {"store_name": store_name, "days": days, "record_count": len(rows),
              "min_samples": TREND_MIN_SAMPLES}
    if not rows:
        trends["overall"] = None
        return trends

    _mark_edge_units(rows)
    dates = sorted({r["date"] for r in rows})
    trends["overall"] = _summarize_unit_rows(rows)
    trends["first_date"] = dates[0]
    trends["last_date"] = dates[-1]
    trends["day_count"] = len(dates)
    trends["unit_count"] = len({r["machine_number"] for r in rows})

    def _grouped(key_func, label_func, sort_key=None, min_count=1):
        buckets = {}
        for row in rows:
            key = key_func(row)
            if key is None:
                continue
            buckets.setdefault(key, []).append(row)
        result = []
        for key, group_rows in buckets.items():
            if len(group_rows) < min_count:
                continue
            summary = _summarize_unit_rows(group_rows)
            summary["key"] = key
            summary["label"] = label_func(key, group_rows)
            result.append(summary)
        result.sort(key=sort_key or (lambda row: -row["avg_diff"]))
        return result

    trends["by_number_suffix"] = _grouped(
        lambda r: int(r["machine_number"][-1]) if r["machine_number"].isdigit() else None,
        lambda key, _rows: f"末尾{key}",
        sort_key=lambda row: row["key"],
    )
    trends["by_edge"] = _grouped(
        lambda r: r.get("is_edge"),
        lambda key, _rows: "端台(角台の候補)" if key else "島の中ほど",
        sort_key=lambda row: 0 if row["key"] else 1,
    )
    trends["by_machine"] = _grouped(
        lambda r: r.get("machine_name") or None,
        lambda key, _rows: key,
    )
    # 台番号ごとの成績(複数日ぶんまとめて、平均差枚の高い順)
    trends["top_units"] = _grouped(
        lambda r: r["machine_number"],
        lambda key, group_rows: f"No.{key}" + (f" {group_rows[0].get('machine_name', '')}" if group_rows[0].get("machine_name") else ""),
    )[:20]

    def _best(rows_):
        eligible = [r for r in rows_ if r["enough_samples"]]
        return max(eligible, key=lambda r: r["avg_diff"]) if eligible else None

    trends["highlights"] = {
        "best_suffix": _best(trends["by_number_suffix"]),
        "best_machine": _best(trends["by_machine"]),
        "edge": next((r for r in trends["by_edge"] if r["key"]), None),
        "middle": next((r for r in trends["by_edge"] if not r["key"]), None),
    }
    return trends


def describe_store_unit_trends(trends, limit=10):
    """台別データの集計を、AIプロンプト用のテキストにまとめる。"""
    if not trends or not trends.get("record_count"):
        return "登録なし"

    overall = trends["overall"]
    lines = [
        f"対象: {trends['first_date']}〜{trends['last_date']} の{trends['day_count']}日分 / "
        f"のべ{trends['record_count']}台(実台数{trends['unit_count']}台)",
        f"全体: 平均差枚{overall['avg_diff']:+.0f}枚, プラス率{overall['plus_rate']:.0f}%, "
        f"平均{overall['avg_games']:.0f}G(差枚が入っている台{overall['diff_count']}件)",
    ]

    def _rows_text(title, rows):
        if not rows:
            return f"{title}: データなし"
        parts = [
            f"{row['label']}: 平均差枚{row['avg_diff']:+.0f}枚/{row['count']}台/プラス率{row['plus_rate']:.0f}%"
            for row in rows[:limit]
        ]
        return f"{title}: " + " / ".join(parts)

    lines.append(_rows_text("台番号末尾別", trends.get("by_number_suffix", [])))
    lines.append(_rows_text("端台(角台の候補)と島の中ほど", trends.get("by_edge", [])))
    lines.append(_rows_text("機種別", trends.get("by_machine", [])))
    lines.append(_rows_text("好調な台(平均差枚の高い順)", trends.get("top_units", [])[:5]))
    return "\n".join(lines)


def describe_store_unit_history(store_name, machine_number, days=90, limit=5):
    """
    特定の台(店舗×台番号)の、取り込んだ台別データでの直近の成績を1行にまとめる。
    設定推測のプロンプトに添えて「この台はこの店でどういう扱いの台か」を材料にする。
    データが無い場合は空文字。
    """
    store_name = (store_name or "").strip()
    machine_number = str(machine_number or "").strip()
    if not store_name or not machine_number:
        return ""

    limit_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d") if days > 0 else ""
    rows = [
        r for r in load_store_units(store_name)
        if r["machine_number"] == machine_number and (not limit_date or r["date"] >= limit_date)
    ]
    if not rows:
        return ""

    diffs = [r["difference_slabs"] for r in rows if r.get("difference_slabs") is not None]
    recent = " / ".join(
        f"{r['date']}: {r['total_games']:.0f}G 差枚{r['difference_slabs']:+.0f}枚"
        if r.get("total_games") is not None and r.get("difference_slabs") is not None
        else f"{r['date']}: データ一部なし"
        for r in rows[:limit]
    )
    summary = f"No.{machine_number}のホールデータ実績({len(rows)}日分)"
    if diffs:
        plus_rate = sum(1 for d in diffs if d > 0) / len(diffs) * 100
        summary += f": 平均差枚{sum(diffs) / len(diffs):+.0f}枚, プラスの日{plus_rate:.0f}%"
    return f"{summary} / 直近: {recent}"
