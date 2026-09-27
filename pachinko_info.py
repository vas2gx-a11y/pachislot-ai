"""
パチンコの機種データ(pachinko_data/*.json)を読む層。パチンコの機種データはここが唯一の置き場所。

スロットの machine_info.py と同じく、JSONを直してpushするのが唯一の更新手段。
以前は画面から登録・編集できるようシート(pachinko_machines)に置いていたが、
実際の運用は「チャットで調べてJSONを書く」だけで、シートに入れる工程が余計な手間になっていた。
Renderのディスクは揮発性なので、画面から書き換える手段は持たない。

スロットの解析まとめ(machine_data/)と分けているのは、項目がまったく違うため
(パチンコは設定差ではなくボーダー・遊タイム・止め打ちがほしい)。書き方は pachinko_data/README.md。
"""

import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "pachinko_data")

SPEC_TYPES = ["ミドル", "ライトミドル", "甘デジ", "ライト", "スマパチ", "その他"]
TEXT_FIELDS = ["name", "maker", "spec_type", "yutime_note", "morning_lamp_note",
               "technique_note", "note", "source"]
# 数値の項目。空は None のまま持ち、「未登録」と「0」を区別する
NUMERIC_FIELDS = [
    "hit_prob", "rush_hit_prob", "rush_entry_rate", "rush_continue_rate",
    "border_equiv", "border_28", "border_33", "yutime_games", "yutime_spins",
]
# ボーダーの換算は「1,000円で借りた玉を何玉で交換するか」で表す(等価=250玉 / 28玉交換=280玉 / 3.03円=330玉)。
# 登録された3点の間を直線で結んで、任意の交換率のボーダーを概算する(店ごとの交換率に合わせるため)。
# 計算は回転率の計算機(ブラウザ側)で行い、この対応表はページに埋め込んで渡す
BORDER_POINTS = [("border_equiv", 250), ("border_28", 280), ("border_33", 330)]


def _path_of(machine_id):
    # URLから来たIDでファイルを開くので、ディレクトリの外を指せないよう文字種を絞る。
    # _ で始まるファイル(ひな形)は一覧に出さないだけでなく、URLからも開けないようにする
    if not machine_id or machine_id.startswith("_") or not all(c.isascii() and (c.isalnum() or c == "_") for c in machine_id):
        return None
    return os.path.join(DATA_DIR, f"{machine_id}.json")


def machine_ids():
    """先頭が _ のファイル(ひな形など)は機種として扱わない。"""
    if not os.path.isdir(DATA_DIR):
        return []
    return sorted(f[:-5] for f in os.listdir(DATA_DIR) if f.endswith(".json") and not f.startswith("_"))


def _to_float_or_none(value):
    text = str(value if value is not None else "").strip().replace(",", "")
    # 大当り確率は「1/319.7」と書かれることが多いので、分母だけ取る
    if text.startswith("1/"):
        text = text[2:]
    try:
        return float(text)
    except ValueError:
        return None


def _normalize(raw, machine_id):
    """手やAIで書いたJSONの揺れ(数値が文字列、項目の欠け)を吸収して、画面が扱う形にそろえる。"""
    machine = {"machine_id": machine_id}
    for field in TEXT_FIELDS:
        machine[field] = str(raw.get(field) or "").strip()
    for field in NUMERIC_FIELDS:
        machine[field] = _to_float_or_none(raw.get(field))
    machine["effects"] = [
        {"name": str(e.get("name")).strip(), "rate": _to_float_or_none(e.get("rate")),
         "note": str(e.get("note") or "").strip()}
        for e in raw.get("effects") or []
        if isinstance(e, dict) and str(e.get("name") or "").strip()
    ]
    machine["updated_at"] = str(raw.get("updated_at") or "").strip()
    return machine


def load(machine_id):
    """機種JSONを読む。無ければ None。壊れたJSONは例外のまま上げる(黙って空ページにしない)。"""
    path = _path_of(machine_id)
    if path is None or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return _normalize(json.load(f), machine_id)


def load_all():
    """一覧用。名前順。読めないファイルは一覧から外し、理由を併せて返す。"""
    machines, errors = [], []
    for mid in machine_ids():
        try:
            machine = load(mid)
        except (OSError, ValueError) as e:
            errors.append(f"{mid}.json: {e}")
            continue
        if not machine["name"]:
            errors.append(f"{mid}.json: name がありません")
            continue
        machines.append(machine)
    machines.sort(key=lambda m: m["name"])
    return machines, errors
