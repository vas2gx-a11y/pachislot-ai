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
    machine["hit_distribution"] = _distribution_groups(raw.get("hit_distribution"))
    machine["sns_tips"] = _sns_tips(raw.get("sns_tips"))
    machine["updated_at"] = str(raw.get("updated_at") or "").strip()
    return machine


# Xの投稿から拾った情報の種類。スロット(machine_data の sns_tips)の3種類に、
# パチンコで解析サイトに載りにくい「朝一ランプ・セグ」と「止め打ち・技術介入」を足している
SNS_KINDS = {
    "aim": "狙い目",
    "quit": "やめどき",
    "lamp": "朝一・ランプ・セグ",
    "technique": "止め打ち・技術介入",
    "note": "その他",
}


def _sns_tips(rows):
    """
    Xの投稿の要約。解析サイトの値とは混ぜず、画面では「非公式」として別枠に出す
    (個人の実戦値や考察が、解析値と同じ重みで読まれないように)。種類の並びは SNS_KINDS の順。
    """
    order = list(SNS_KINDS)
    tips = [
        {"kind": r.get("kind") if r.get("kind") in SNS_KINDS else "note",
         "text": str(r.get("text") or "").strip(),
         "url": str(r.get("url") or "").strip(),
         "account": str(r.get("account") or "").strip(),
         "date": str(r.get("date") or "").strip()}
        for r in rows or [] if isinstance(r, dict) and str(r.get("text") or "").strip()
    ]
    for t in tips:
        t["label"] = SNS_KINDS[t["kind"]]
    return sorted(tips, key=lambda t: order.index(t["kind"]))


# 円グラフの色。DMMぱちタウンの振り分け図と同じく、そこで終わる振り分け(時短なし・通常へ戻る)は青、
# 続く振り分けは暖色系にして、「当たっても終わる割合」がひと目で分かるようにする
PIE_END_COLOR = "#2f5fd0"
PIE_COLORS = ["#e8962e", "#d93a2b", "#8e3fb8", "#3f9a4a", "#e05cb0", "#e2b21f", "#1a9c93", "#8a5a33"]
_END_WORDS = ("時短なし", "通常時", "通常へ")


def _with_pie(group):
    """状態ごとの円グラフ(CSS の conic-gradient)と、各行の凡例の色を付ける。"""
    total = group["total"] or 0
    start, stops, color_index = 0.0, [], 0
    for r in group["rows"]:
        if any(w in r["next"] for w in _END_WORDS):
            r["color"] = PIE_END_COLOR
        else:
            r["color"] = PIE_COLORS[color_index % len(PIE_COLORS)]
            color_index += 1
        # 合計が100でない(読み取りミスの疑いがある)状態でも円が欠けないよう、合計に対する割合で描く
        end = start + (r["rate"] or 0) / total * 100 if total else start
        stops.append(f"{r['color']} {start:.2f}% {end:.2f}%")
        start = end
    group["pie"] = f"conic-gradient({', '.join(stops)})" if stops and total else ""
    return group


def _distribution_groups(rows):
    """
    大当り振り分けを状態(ヘソ・電チューなど)ごとに束ねる。

    JSONは1行1振り分けの平らな並びで書く(AIに書かせやすく、差分も読みやすいため)が、
    画面では状態ごとの表にしたいので、書かれた順を保ったままここで束ねる。
    合計が100%にならない状態は読み取りミスの可能性が高いので、total を持たせて画面で気づけるようにする。
    """
    groups = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        state = str(r.get("state") or "").strip() or "—"
        if not groups or groups[-1]["state"] != state:
            groups.append({"state": state, "rows": [], "total": 0.0})
        rate = _to_float_or_none(r.get("rate"))
        groups[-1]["rows"].append({
            "rate": rate,
            "rounds": str(r.get("rounds") or "").strip(),
            "payout": str(r.get("payout") or "").strip(),
            "next": str(r.get("next") or "").strip(),
            "note": str(r.get("note") or "").strip(),
        })
        groups[-1]["total"] += rate or 0
    for g in groups:
        g["total"] = round(g["total"], 2)
        _with_pie(g)
    return groups


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
