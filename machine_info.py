"""
機種情報(解析まとめ)のJSONを読む層。機種のデータはここが唯一の置き場所。

【なぜJSONファイルなのか】
機種情報は「新台が出るたびに1ファイル足す」運用で、AIに資料を渡して
machine_data/<id>.json を作らせる前提にしている。ファイルならAIの出力をそのまま置けて、
差分もgitで追える。画面(/info)はこのJSONを毎回読んで描くだけなので、
ファイルを置けば再起動なしでページが増える。

【なぜ1か所にまとめたのか】
以前は用途ごとに machines シート(AIの設定推測用)・SQLite(設定判別用)・このJSON(表示用)の
3か所に機種データがあり、同じ機種の数値を3回登録して、どれが正か分からなくなっていた。
いまは判別(to_client_spec)も設定推測・Q&A(to_rule)も、このJSONから都度組み立てる。
画面から機種データを編集する手段は持たない。Renderのディスクは揮発性で、
サーバー上で書き換えても再デプロイで消えるため、JSONを直してデプロイするのが唯一の更新手段。
"""

import json
import os
import re
import unicodedata

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "machine_data")

ALL_SETTINGS = ["1", "2", "3", "4", "5", "6"]


def _path_of(machine_id):
    # URLから来たIDでファイルを開くので、ディレクトリの外を指せないよう文字種を絞る
    if not machine_id or not all(c.isascii() and (c.isalnum() or c == "_") for c in machine_id):
        return None
    return os.path.join(DATA_DIR, f"{machine_id}.json")


def machine_ids():
    """先頭が _ のファイル(ひな形など)は機種として扱わない。"""
    if not os.path.isdir(DATA_DIR):
        return []
    return sorted(
        f[:-5] for f in os.listdir(DATA_DIR)
        if f.endswith(".json") and not f.startswith("_") and f != "schema.json"
    )


def load(machine_id):
    """機種JSONを読む。無ければ None。壊れたJSONは例外のまま上げる(黙って空ページにしない)。"""
    path = _path_of(machine_id)
    if path is None or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_all():
    """一覧用。読めないファイルは一覧から外し、理由を併せて返す。"""
    machines, errors = [], []
    for mid in machine_ids():
        try:
            machines.append(load(mid))
        except (OSError, ValueError) as e:
            errors.append(f"{mid}.json: {e}")
    machines.sort(key=lambda m: str(m.get("basic", {}).get("release_date") or ""), reverse=True)
    return machines, errors


def validate(m, machine_id):
    """JSONを手やAIで書く前提なので、描画前に食い違いを拾って画面に出す。"""
    problems = []
    for key in ("id", "name", "updated_at", "sources", "basic", "settings"):
        if key not in m:
            problems.append(f"必須項目「{key}」がありません")
    if m.get("id") and m["id"] != machine_id:
        problems.append(f"id「{m['id']}」とファイル名が一致しません")

    source_ids = {s.get("id") for s in m.get("sources", [])}
    settings = set((m.get("settings") or {}).get("list", []))

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "source_ids" or k == "hints_source_ids":
                    for sid in v or []:
                        if sid not in source_ids:
                            problems.append(f"出典「{sid}」が sources にありません")
                elif k == "values" and isinstance(v, dict) and settings:
                    for s in v:
                        if s not in settings:
                            problems.append(f"搭載されていない設定「{s}」の値があります")
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(m)
    return sorted(set(problems))


# ---------------------------------------------------------------------------
# 設定判別(ブラウザのベイズ推定)の形への変換
# ---------------------------------------------------------------------------
# 判別エンジン(static/js/bayes.js)は設定1〜6の6要素がそろっている前提。
# 設定1が無い機種などは、非搭載の設定を availableSettings で候補から外したうえで、
# 要素を埋めるために「搭載されている中で最も低い設定」の値を入れる。
# 事後確率が0に固定されるので計算には効かず、判別力の目安(設定1と6の比較)が
# 「最低設定と6の比較」として自然に読めるようになる。

def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _six(values, available):
    """設定別の値を1〜6の6要素に揃える。搭載設定に欠けがあれば None(=判別に使えない)。"""
    if not isinstance(values, dict):
        return None
    got = {s: _num(values.get(s)) for s in available}
    if any(v is None for v in got.values()):
        return None
    lowest = got[available[0]]
    return [got.get(s, lowest) for s in ALL_SETTINGS]


def to_judge_spec(m):
    """
    機種JSONから判別に使う要素だけを抜き出す。
    変換できなかった要素は黙って捨てず、skipped に理由を入れて返す(機種情報ページに出す)。
    """
    est = m.get("setting_estimation") or {}
    skipped = []

    # judge_exclude は「搭載はされているが判別では候補から外す設定」。
    # 数値が調査中の設定1でも、下パネルの点灯などで否定できる機種があり、
    # その場合は残りの設定だけで判別した方が、欠けた数値を推測で埋めるより正確になる。
    excluded = set((est.get("judge_exclude") or {}).get("settings", []))
    available = [s for s in ALL_SETTINGS
                 if s in (m.get("settings") or {}).get("list", []) and s not in excluded]
    if not available:
        raise ValueError("判別に使える設定がありません（settings.list / judge_exclude を確認）")

    payout_row = next((r for r in (m.get("spec") or {}).get("rows", []) if r.get("key") == "payout"), None)
    payouts = _six(payout_row.get("values"), available) if payout_row else None
    if payouts is None:
        payouts = [None] * 6
        skipped.append("機械割（設定別の値が揃っていないため未登録）")

    judge_items = []
    for p in est.get("probabilities", []):
        if p.get("judge") is False:
            continue  # 分母が不明確など、JSON側で判別に使わないと決めた要素
        denominators = _six(p.get("values"), available)
        if denominators is None or any(d <= 1 for d in denominators):
            skipped.append(f"{p.get('label')}（値が揃っていない）")
            continue
        judge_items.append({
            "name": p["label"],
            # 分母が大きい(1/100超)ものはボーナス扱い。判別画面の並び・表示の区別に使われる
            "item_type": "bonus" if min(denominators) > 100 else "koyaku",
            "denom_base": p.get("denom_base") if p.get("denom_base") in ("total", "normal") else "total",
            "denominators": denominators,
        })

    categorical_groups = []
    for r in est.get("ratios", []):
        if r.get("judge") is False:
            continue
        options = []
        for o in r.get("outcomes", []):
            probs = _six(o.get("values"), available)
            if probs is None:
                skipped.append(f"{r.get('label')} / {o.get('label')}（値が揃っていない）")
                continue
            options.append({"name": o["label"], "probabilities": probs})
        if len(options) < 2:
            skipped.append(f"{r.get('label')}（選択肢が2つ未満）")
            continue
        categorical_groups.append({"name": r["label"], "options": options})

    confirmations = []
    for h in est.get("hints", []):
        exact, minimum = h.get("exact_setting"), h.get("min_setting")
        denied = set(h.get("denied_settings") or [])
        if not (exact or minimum or denied):
            continue  # 「高設定示唆」のような重み付けだけの示唆は確定演出として扱えない
        flags = []
        for i in range(1, 7):
            ok = (i == exact) if exact else (i >= minimum if minimum else True)
            flags.append(0 if (not ok or i in denied) else 1)
        # 候補外の設定は判別画面で常に外れるので、それを除いても何も絞れない演出
        # (例: 設定1濃厚の演出で、設定1自体を候補外にしている)は取り込まない
        flags = [f if ALL_SETTINGS[i] in available else 0 for i, f in enumerate(flags)]
        if not any(flags):
            skipped.append(f"{h['category']} / {h['pattern']}（判別の候補に残る設定が無い）")
            continue
        if all(flags[i] for i, s in enumerate(ALL_SETTINGS) if s in available):
            continue
        confirmations.append({"group": h["category"], "name": h["pattern"], "flags": flags})

    return {
        "name": m["name"],
        "maker": (m.get("basic") or {}).get("maker") if isinstance((m.get("basic") or {}).get("maker"), str) else None,
        "payouts": payouts,
        "available_settings": "".join("1" if s in available else "0" for s in ALL_SETTINGS),
        "judge_items": judge_items,
        "categorical_groups": categorical_groups,
        "confirmations": confirmations,
    }, skipped


def to_client_spec(m):
    """
    判別ページにそのまま埋め込める形に変換する。戻り値は (spec, skipped)。
    判別に使える要素が1つも無い機種は spec が None(判別の機種一覧に出さない)。
    """
    spec, skipped = to_judge_spec(m)
    if not (spec["judge_items"] or spec["categorical_groups"] or spec["confirmations"]):
        return None, skipped
    return {
        # 判別ログは機種名で紐づけているので、IDはファイル名でよい(DB時代の連番とは互換がない)
        "id": m["id"],
        "name": spec["name"],
        "maker": spec["maker"],
        "availableSettings": [c == "1" for c in spec["available_settings"]],
        # 1つでも欠けていると時給計算が破綻するので、全部揃っているときだけ渡す
        "payouts": spec["payouts"] if all(p is not None for p in spec["payouts"]) else None,
        "judgeItems": [
            {"name": j["name"], "type": j["item_type"], "denomBase": j["denom_base"],
             "denominators": j["denominators"]}
            for j in spec["judge_items"]
        ],
        "categoricalGroups": spec["categorical_groups"],
        "confirmations": [
            {"group": c["group"], "name": c["name"], "flags": [bool(f) for f in c["flags"]]}
            for c in spec["confirmations"]
        ],
    }, skipped


def load_for_judge():
    """判別ページ用に全機種を変換する。JSONに不備がある機種は、誤った数値で判別しないよう外す。"""
    machines, _ = load_all()
    result = []
    for m in machines:
        if validate(m, m.get("id")):
            continue
        try:
            spec, _ = to_client_spec(m)
        except ValueError:
            continue
        if spec:
            result.append(spec)
    return sorted(result, key=lambda s: s["name"])


# ---------------------------------------------------------------------------
# 機種名での検索
# ---------------------------------------------------------------------------
# 記録の登録画面では機種名を手入力するので、正式名と一致しないことが多い。
# 正式名と aliases(通称)の両方で探し、完全一致 → 部分一致(長く一致した方)の順に採用する。
# 部分一致を「どちらかがどちらかを含む」の双方向にしているのは、
# 「東京喰種」(入力)⊂「L 東京喰種」(正式名) と、逆に通称の方が短いケースの両方があるため。
# 比較の前に全角半角・大文字小文字・空白をそろえる(「スマスロ 東京喰種」と「スマスロ東京喰種」を同じに扱う)。

# 部分一致に使う名前の最短の長さ。「TG」のような短い通称が無関係な機種名に紛れて当たるのを防ぐ
_MIN_PARTIAL_LEN = 3


def _normalize(name):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", name)).lower()


def _names_of(m):
    return [_normalize(n) for n in [m.get("name")] + list(m.get("aliases") or [])
            if isinstance(n, str) and n.strip()]


def find_by_name(name):
    """機種名(表記ゆれ込み)から機種JSONを探す。見つからなければ None。"""
    name = _normalize(name or "")
    if not name:
        return None
    machines, _ = load_all()

    for m in machines:
        if name in _names_of(m):
            return m

    best, best_len = None, 0
    for m in machines:
        for n in _names_of(m):
            if min(len(n), len(name)) < _MIN_PARTIAL_LEN:
                continue
            if n in name or name in n:
                overlap = min(len(n), len(name))
                if overlap > best_len:
                    best, best_len = m, overlap
    return best


# ---------------------------------------------------------------------------
# AIの設定推測・Q&A・期待値概算に渡す形への変換
# ---------------------------------------------------------------------------
# common.py の設定推測は、もともと machines シートの
#   hint_words(強示唆ワード) / game_flow(仕様の説明文) / setting_ratios(設定別確率表)
#   / suggestion_items(記録時に入力させる示唆項目)
# の4つを前提に組まれている。プロンプトや登録画面の入力欄はそのまま使えるので、
# JSONからこの4つを組み立てて渡す(呼び出し側を書き換えずに置き場所だけ移すため)。

# game_flow としてAIに渡すセクション。設定差の数値は setting_ratios で別に渡すので含めない
_GAME_FLOW_KEYS = ("features", "ceiling", "zones", "quit_timing", "reset",
                   "favorable_zone", "practical_points", "cautions")

# 記録の登録画面に出す示唆項目の重要度(0〜100)。AIへのプロンプトにそのまま載る
_WEIGHT_CONFIRM = 100  # 設定◯以上濃厚・設定◯確定
_WEIGHT_DENY = 70      # 設定◯否定
_WEIGHT_COUNT = 60     # 回数を数えて設定差を見る要素


def _fmt_value(v, fmt):
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    if fmt == "fraction":
        return f"1/{v}"
    if fmt == "percent":
        return f"{v}%"
    return str(v)


def _setting_ratios(m):
    """{"1": {"機械割": "97.5%", "AT初当り": "1/394.4", ...}, ...} の形にする。"""
    # setting_estimation.probabilities は判別(to_judge_spec)と同じく、format が無くても分母として読む
    rows = [(r, r.get("format")) for r in (m.get("spec") or {}).get("rows", [])]
    rows += [(p, p.get("format") or "fraction")
             for p in (m.get("setting_estimation") or {}).get("probabilities", [])]
    ratios = {}
    for r, fmt in rows:
        if not isinstance(r.get("values"), dict):
            continue
        for setting, v in r["values"].items():
            text = _fmt_value(v, fmt)
            if text:
                ratios.setdefault(setting, {})[r.get("label") or r.get("key")] = text
    return ratios


def _hint_label(h):
    return f"{h.get('category')}：{h.get('pattern')}"


def _strong_hints(m):
    """設定を確定・否定できる示唆だけ。重み付けだけの示唆はAIに渡しても判断がぶれるので除く。"""
    return [h for h in (m.get("setting_estimation") or {}).get("hints", [])
            if h.get("exact_setting") or h.get("min_setting") or h.get("denied_settings")]


def to_rule(m):
    """機種JSONを、設定推測(common.estimate など)が使う形に変換する。"""
    basic = m.get("basic") or {}
    flow = {"type": basic.get("type"), "summary": basic.get("summary")}
    flow.update({k: m[k] for k in _GAME_FLOW_KEYS if m.get(k)})

    strong = _strong_hints(m)
    suggestion_items = [
        {"name": _hint_label(h), "type": "boolean",
         "weight": _WEIGHT_CONFIRM if (h.get("exact_setting") or h.get("min_setting")) else _WEIGHT_DENY}
        for h in strong
    ]
    suggestion_items += [
        {"name": p.get("label"), "type": "count", "weight": _WEIGHT_COUNT}
        for p in (m.get("setting_estimation") or {}).get("probabilities", [])
        if p.get("label") and p.get("judge") is not False
    ]

    return {
        # スクショの文字やメモにこの文言が含まれていたら強示唆として扱う(common.estimate)
        "hint_words": list(dict.fromkeys(h["pattern"] for h in strong if h.get("pattern"))),
        "game_flow": json.dumps(flow, ensure_ascii=False),
        "setting_ratios": _setting_ratios(m),
        "suggestion_items": suggestion_items,
        "sources": m.get("sources", []),
    }
