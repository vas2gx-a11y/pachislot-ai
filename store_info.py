"""
店舗情報(住所・台数・営業時間などの基本情報)のJSONを読み書きする層。

【なぜJSONファイルなのか】
機種情報(machine_info.py)と同じく「1店舗1ファイル」で、Webから集めた結果を
store_data/<id>.json に置く運用にしている。項目ごとに出典と確認状態を持たせたいので
シートの1行には収まらず、差分もgitで追える方が都合がいい。
画面(/store_info)はこのJSONを毎回読んで描くだけ。

【営業データとの分担】
日別・台別の営業データ(差枚・G数・BB/RB など)は、これまで通りシート側が正。
JSONには持たせず、sheet_store_name でシートの店舗名と紐づけて画面で並べる。
基本情報は「たまに変わる」、営業データは「毎日増える」もので、置き場所を分けておくと
設定推測(common.py)からは今まで通りシートだけを見ればよい。

【更新の考え方】
Webから集めた値は merge() で既存JSONに重ねる。
  - 値が変わった項目だけ書き換え、変更前・変更後・日時・出典を history に残す
  - 集めたページに書かれていなかった項目(値が None)は、消えたとは限らないので触らない
  - 推測で埋めた値を入れないよう、確認状態(status)は出典の種類からコード側で決める
"""

import json
import os
from datetime import date

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "store_data")

# 項目の定義。並びがそのまま画面・収集プロンプトの並びになる。
# kind は値の形:
#   text     文字列
#   int      整数
#   list     文字列の配列
#   links    [{"label": "X(旧Twitter)", "url": "https://..."}]
#   machines [{"name": "機種名", "units": 20}]   units は不明なら null
#   dated    [{"date": "2026-09-01", "title": "...", "detail": "..."}]   date は不明なら null
FIELDS = [
    ("city", "市区町村", "text"),
    ("address", "住所", "text"),
    ("phone", "電話番号", "text"),
    ("hours", "営業時間", "text"),
    ("total_units", "総台数", "int"),
    ("pachinko_units", "パチンコ台数", "int"),
    ("slot_units", "パチスロ台数", "int"),
    ("rates", "貸玉・貸メダル料金", "list"),
    ("parking", "駐車場", "text"),
    ("access", "アクセス", "text"),
    ("official_url", "公式サイト", "text"),
    ("sns", "SNS", "links"),
    ("line", "LINE", "text"),
    ("features", "店舗の特徴", "list"),
    ("installed_machines", "設置機種（パチスロ）", "machines"),
    ("special_days", "営業日・特定日", "list"),
    ("new_machines", "新台入替", "dated"),
    ("notices", "イベント・告知", "dated"),
]
FIELD_KEYS = [k for k, _, _ in FIELDS]
FIELD_LABELS = {k: label for k, label, _ in FIELDS}
FIELD_KINDS = {k: kind for k, _, kind in FIELDS}

# 出典の種類。確認状態はここから決める(AIに「確認済か」を判断させると根拠が曖昧になるため)。
#   公式       店舗の公式サイト・公式SNS → 確認済
#   ポータル   P-WORLD などの店舗情報サイト → 未確認(更新が遅れていることがある)
#   その他     ブログ・まとめ・手入力のメモなど → 未確認
RELIABILITIES = ("公式", "ポータル", "その他")
STATUSES = ("確認済", "未確認", "不明")


def status_for(reliability):
    return "確認済" if reliability == "公式" else "未確認"


def today():
    return date.today().isoformat()


def _path_of(store_id):
    # URLから来たIDでファイルを開くので、ディレクトリの外を指せないよう文字種を絞る
    if not store_id or not all(c.isascii() and (c.isalnum() or c == "_") for c in store_id):
        return None
    return os.path.join(DATA_DIR, f"{store_id}.json")


def store_ids():
    """先頭が _ のファイル(ひな形など)は店舗として扱わない。"""
    if not os.path.isdir(DATA_DIR):
        return []
    return sorted(
        f[:-5] for f in os.listdir(DATA_DIR)
        if f.endswith(".json") and not f.startswith("_") and f != "schema.json"
    )


def load(store_id):
    """店舗JSONを読む。無ければ None。壊れたJSONは例外のまま上げる(黙って空ページにしない)。"""
    path = _path_of(store_id)
    if path is None or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_all():
    """一覧用。読めないファイルは一覧から外し、理由を併せて返す。"""
    stores, errors = [], []
    for sid in store_ids():
        try:
            stores.append(load(sid))
        except (OSError, ValueError) as e:
            errors.append(f"{sid}.json: {e}")
    stores.sort(key=lambda s: (s.get("region") or "", s.get("name") or ""))
    return stores, errors


def save(store):
    path = _path_of(store.get("id"))
    if path is None:
        raise ValueError(f"店舗IDが不正です: {store.get('id')!r}（英小文字・数字・_ のみ）")
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return path


def new_store(store_id, name, region, sheet_store_name=""):
    """
    店舗名と地域だけの空の店舗を作る。項目はすべて「不明」から始め、
    収集した値で埋まった分だけ「確認済」「未確認」になる。
    """
    return {
        "$schema": "./schema.json",
        "id": store_id,
        "name": name,
        "region": region,
        # シート側(日別・台別データ)の店舗名。表記が違う場合だけ変える
        "sheet_store_name": sheet_store_name or name,
        "updated_at": today(),
        "sources": [],
        "info": {k: {"value": None, "status": "不明", "source_ids": []} for k in FIELD_KEYS},
        "history": [],
    }


# ---------------------------------------------------------------------------
# 検証
# ---------------------------------------------------------------------------
# JSONを手やAIで書く前提なので、描画前に食い違いを拾って画面に出す。

def _is_empty(v):
    return v is None or v == "" or v == []


def _kind_ok(kind, v):
    if kind == "text":
        return isinstance(v, str)
    if kind == "int":
        return isinstance(v, int) and not isinstance(v, bool)
    if kind == "list":
        return isinstance(v, list) and all(isinstance(x, str) for x in v)
    if kind == "links":
        return isinstance(v, list) and all(isinstance(x, dict) and x.get("url") for x in v)
    if kind == "machines":
        return isinstance(v, list) and all(isinstance(x, dict) and x.get("name") for x in v)
    if kind == "dated":
        return isinstance(v, list) and all(isinstance(x, dict) and x.get("title") for x in v)
    return False


def validate(store, store_id):
    problems = []
    for key in ("id", "name", "region", "updated_at", "sources", "info", "history"):
        if key not in store:
            problems.append(f"必須項目「{key}」がありません")
    if store.get("id") and store["id"] != store_id:
        problems.append(f"id「{store['id']}」とファイル名が一致しません")

    source_ids = set()
    for s in store.get("sources", []):
        source_ids.add(s.get("id"))
        if s.get("reliability") not in RELIABILITIES:
            problems.append(f"出典「{s.get('id')}」の reliability は {' / '.join(RELIABILITIES)} のいずれか")
        if not s.get("checked_at"):
            problems.append(f"出典「{s.get('id')}」に checked_at がありません")

    info = store.get("info") or {}
    for key in info:
        if key not in FIELD_KINDS:
            problems.append(f"未定義の項目「{key}」があります")
    for key in FIELD_KEYS:
        item = info.get(key)
        if not isinstance(item, dict):
            problems.append(f"項目「{FIELD_LABELS[key]}」がありません")
            continue
        v, status = item.get("value"), item.get("status")
        if status not in STATUSES:
            problems.append(f"「{FIELD_LABELS[key]}」の status は {' / '.join(STATUSES)} のいずれか")
        if _is_empty(v):
            if status != "不明":
                problems.append(f"「{FIELD_LABELS[key]}」は値が空なのに status が「{status}」です")
        else:
            if not _kind_ok(FIELD_KINDS[key], v):
                problems.append(f"「{FIELD_LABELS[key]}」の値の形が違います（{FIELD_KINDS[key]}）")
            if status == "不明":
                problems.append(f"「{FIELD_LABELS[key]}」は値があるのに status が「不明」です")
            if not item.get("source_ids"):
                # 出典の無い値は推測と区別できないので、必ず指摘する
                problems.append(f"「{FIELD_LABELS[key]}」に出典(source_ids)がありません")
        for sid in item.get("source_ids") or []:
            if sid not in source_ids:
                problems.append(f"出典「{sid}」が sources にありません")

    for h in store.get("history", []):
        for sid in h.get("source_ids") or []:
            if sid not in source_ids:
                problems.append(f"履歴の出典「{sid}」が sources にありません")
    return sorted(set(problems))


# ---------------------------------------------------------------------------
# 収集結果の反映
# ---------------------------------------------------------------------------

def upsert_source(store, url, name, reliability, checked_at=None):
    """
    出典を登録して id を返す。同じURLなら確認日だけ更新する
    (同じページを取り直すたびに出典が増えていかないように)。
    """
    checked_at = checked_at or today()
    for s in store["sources"]:
        if url and s.get("url") == url:
            s["checked_at"] = checked_at
            s["name"] = name or s.get("name")
            s["reliability"] = reliability
            return s["id"]
    used = {s.get("id") for s in store["sources"]}
    n = len(store["sources"]) + 1
    while f"s{n}" in used:
        n += 1
    sid = f"s{n}"
    store["sources"].append({
        "id": sid, "name": name or url, "url": url,
        "reliability": reliability, "checked_at": checked_at,
    })
    return sid


def _normalize(kind, v):
    """表記の揺れ(前後の空白・空文字)で「変更あり」と判定しないように揃える。"""
    if kind == "text":
        v = (v or "").strip() if isinstance(v, str) else v
    return None if _is_empty(v) else v


def diff(store, values, reliability):
    """
    収集した値と今の値を比べ、変わる項目だけを返す(保存はしない)。
    values は {項目キー: 値}。None / 空の項目は「そのページに書かれていなかった」扱いで比べない。

    公式で確認済みの値と、公式以外の出典の値が食い違う場合は conflict として返し、反映しない。
    ポータルサイトは更新が遅れがちで、定期的に取り直すたびに古い値へ戻ってしまうため。
    """
    changes = []
    info = store["info"]
    weaker = status_for(reliability) != "確認済"
    for key in FIELD_KEYS:
        if key not in values:
            continue
        new = _normalize(FIELD_KINDS[key], values[key])
        if new is None:
            continue
        item = info.get(key) or {}
        old = _normalize(FIELD_KINDS[key], item.get("value"))
        if new != old:
            conflict = weaker and item.get("status") == "確認済"
            changes.append({"field": key, "before": old, "after": new, "conflict": conflict})
    return changes


def merge(store, values, source_id, reliability, changed_at=None):
    """
    収集した値を店舗JSONに重ねる。変わった項目は history に残し、
    同じ値が別の出典でも確認できた項目は出典を足す(公式で確認できれば確認済に上げる)。
    戻り値は反映した変更の一覧(conflict は含まない)。
    """
    changed_at = changed_at or today()
    status = status_for(reliability)
    all_changes = diff(store, values, reliability)
    changes = [c for c in all_changes if not c["conflict"]]
    changed_keys = {c["field"] for c in all_changes}

    for c in changes:
        store["info"][c["field"]] = {"value": c["after"], "status": status, "source_ids": [source_id]}
        store["history"].append({
            "field": c["field"], "before": c["before"], "after": c["after"],
            "changed_at": changed_at, "source_ids": [source_id],
        })

    for key in FIELD_KEYS:
        if key in changed_keys or key not in values:
            continue
        new = _normalize(FIELD_KINDS[key], values[key])
        item = store["info"].setdefault(key, {"value": None, "status": "不明", "source_ids": []})
        if new is None or new != _normalize(FIELD_KINDS[key], item.get("value")):
            continue
        if source_id not in item["source_ids"]:
            item["source_ids"].append(source_id)
        if status == "確認済":
            item["status"] = "確認済"

    store["updated_at"] = changed_at
    return changes
