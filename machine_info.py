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
    problems += flow_problems(m.get("game_flow"))
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

# game_flow としてAIに渡すセクション。設定差の数値は setting_ratios で別に渡すので含めない。
# JSONの game_flow(フロー図の箱と矢印)は「何から何へ移るか」をそのまま表しているので、AIにも渡す
_GAME_FLOW_KEYS = ("game_flow", "features", "ceiling", "zones", "quit_timing", "reset",
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


# ---------------------------------------------------------------------------
# ゲームフロー図(game_flow)のレイアウト
# ---------------------------------------------------------------------------
# 解析サイトの「ゲームフロー」画像と同じく、通常時 → CZ・ボーナス → AT → 特化ゾーン を
# 上から下へ箱と矢印で描く。JSONには箱(nodes)と矢印(edges)だけを書き、座標は持たせない
# (AIに書かせる前提なので、座標まで書かせると崩れた図になりやすい)。
#
# 並べ方は階層型グラフ描画の簡易版:
#   1. 先頭の箱から矢印をたどり、戻り矢印(AT終了→通常時 など)を見つける
#   2. 戻り矢印を除いた最長経路で段を決める(1段とばす矢印は中継点を置いて段ごとに通す)
#   3. 各段は上の段の親の位置の平均順に並べる(同じならJSONの順)
# 戻り矢印は長い線で引き回すと図がスパゲッティになるので、線は引かずに
# 元の箱の下端に「↩ 失敗 → 通常時」と書く。解析サイトの画像もたいていこの書き方をしている。
# 座標はサーバーで決めて SVG で描く(SVGは文字を折り返さないので、折り返しもここで行う)。

FLOW_KINDS = {
    # kind: (箱の帯に出す既定の文言, 帯の色)
    "normal": ("通常時", "#52525b"),
    "cz": ("CZ", "#2563eb"),
    "bonus": ("ボーナス", "#d97706"),
    "at": ("AT", "#dc2626"),
    "special": ("特化ゾーン", "#7c3aed"),
    "other": ("", "#0f766e"),
}

_NODE_W = 144          # 箱の幅
_DUMMY_W = 12          # 段をとばす矢印の中継点の幅
_COL_GAP = 18          # 同じ段の箱どうしの間隔
_ROW_GAP = 58          # 段と段の間隔(矢印のラベルが入る)
_MARGIN = 10
_PAD = 8               # 箱の内側の余白
_BAND_H = 18           # 箱の上の色帯
_LABEL_SIZE, _LABEL_LH = 13, 17
_NOTE_SIZE, _NOTE_LH = 11, 14
_EDGE_LABEL_SIZE = 10.5


def _text_width(s, size):
    """SVGの文字幅の見積もり。全角は1文字=1em、半角は大文字・数字が広めなので分けて見積もる
    (少なめに見積もると太字の「PREMIUM GOD GAME」などが箱からはみ出す)。"""
    def w(c):
        if unicodedata.east_asian_width(c) in "WFA":
            return 1.0
        return 0.7 if c.isupper() or c.isdigit() else 0.58
    return sum(w(c) for c in s) * size


def _wrap(text, size, width):
    """幅に収まるよう折り返す。日本語は1文字単位でよいが、「62.5%」「1/49」のような
    半角の並びは途中で切ると読めないので、ひとかたまりとして扱う。"""
    tokens = re.findall(r"[!-~]+|\n|.", str(text or ""))
    lines, cur = [], ""
    for t in tokens:
        if t == "\n":
            lines.append(cur)
            cur = ""
            continue
        if cur and _text_width(cur + t, size) > width:
            # 句読点・閉じ括弧が行頭に来ると読みにくいので、前の行に付ける
            if t in "、。・）」』】〕":
                cur += t
                continue
            lines.append(cur)
            cur = ""
        # 1かたまりで幅を超える長い半角(URLなど)だけは文字単位で切る
        while _text_width(t, size) > width and len(t) > 1:
            k = len(t)
            while k > 1 and _text_width(t[:k], size) > width:
                k -= 1
            lines.append(t[:k])
            t = t[k:]
        cur += t
    if cur:
        lines.append(cur)
    return lines


def flow_problems(flow):
    """validate() から呼ぶ。矢印の参照先の間違いなど、図が描けなくなる不備を拾う。"""
    if not flow:
        return []
    problems = []
    nodes = flow.get("nodes") or []
    ids = [n.get("id") for n in nodes]
    if not nodes:
        problems.append("game_flow に nodes がありません")
    for i in sorted({i for i in ids if ids.count(i) > 1}, key=str):
        problems.append(f"game_flow の箱のID「{i}」が重複しています")
    for n in nodes:
        if n.get("kind") not in FLOW_KINDS:
            problems.append(f"game_flow の箱「{n.get('id')}」の kind「{n.get('kind')}」は使えません")
    for e in flow.get("edges") or []:
        for end in ("from", "to"):
            if e.get(end) not in ids:
                problems.append(f"game_flow の矢印の {end}「{e.get(end)}」に当たる箱がありません")
    return problems


def _ranks(nodes, edges):
    """戻り矢印を除いた最長経路で段を決める。戻り矢印の判定は先頭の箱からの深さ優先探索。"""
    order = [n["id"] for n in nodes]
    out = {i: [] for i in order}
    for k, e in enumerate(edges):
        out[e["from"]].append(k)

    back, state = set(), {}

    def dfs(v):
        state[v] = 1
        for k in out[v]:
            w = edges[k]["to"]
            if state.get(w) == 1:
                back.add(k)
            elif w not in state:
                dfs(w)
        state[v] = 2

    for v in order:  # 先頭から辿れない箱(書き忘れなど)も図には出す
        if v not in state:
            dfs(v)

    # row は「この段より上には置かない」という下限。突入契機が分からず矢印の入らない箱
    # (上位CZなど)を、先頭の段に浮かせず関係する段の近くに置くために使う
    rows = {n["id"]: n["row"] for n in nodes if isinstance(n.get("row"), int) and n["row"] >= 0}
    rank = {v: rows.get(v, 0) for v in order}
    for _ in order:  # DAGなので段数は箱の数を超えない
        changed = False
        for k, e in enumerate(edges):
            if k not in back and rank[e["to"]] < rank[e["from"]] + 1:
                rank[e["to"]] = rank[e["from"]] + 1
                changed = True
        if not changed:
            break
    return rank


def flow_layout(flow):
    """game_flow を SVG 用の座標に変換する。不備があれば None(ページ上部に不備として出る)。"""
    if not flow or flow_problems(flow):
        return None
    nodes = flow["nodes"]
    edges = [e for e in flow.get("edges") or []]
    rank = _ranks(nodes, edges)
    index = {n["id"]: i for i, n in enumerate(nodes)}

    # 箱の中身(折り返し済みの行)と、戻り矢印の注記
    inner_w = _NODE_W - _PAD * 2
    boxes = {}
    for n in nodes:
        tag, color = FLOW_KINDS[n["kind"]]
        boxes[n["id"]] = {
            "id": n["id"], "kind": n["kind"], "color": color,
            "tag": n.get("tag") or tag,
            "label": _wrap(n.get("label"), _LABEL_SIZE, inner_w),
            "note": _wrap(n.get("note"), _NOTE_SIZE, inner_w),
            "returns": [],
        }
    forward = []
    for e in edges:
        if rank[e["to"]] > rank[e["from"]]:
            forward.append(e)
        else:
            to_label = nodes[index[e["to"]]].get("label") or e["to"]
            text = f"↩ {e['label']} → {to_label}" if e.get("label") else f"↩ {to_label}へ"
            boxes[e["from"]]["returns"] += _wrap(text, _NOTE_SIZE, inner_w)

    # 段をとばす矢印は、間の段に中継点を置く(箱を突っ切って線が引かれないように)
    rows = {}
    for n in nodes:
        rows.setdefault(rank[n["id"]], []).append({"id": n["id"], "w": _NODE_W, "order": index[n["id"]]})
    chains = []
    for k, e in enumerate(forward):
        chain = [e["from"]]
        for r in range(rank[e["from"]] + 1, rank[e["to"]]):
            did = f"_d{k}_{r}"
            rows.setdefault(r, []).append({"id": did, "w": _DUMMY_W, "order": index[e["from"]] + 0.5})
            chain.append(did)
        chain.append(e["to"])
        chains.append(chain)
    preds = {}
    for chain in chains:
        for a, b in zip(chain, chain[1:]):
            preds.setdefault(b, []).append(a)

    # 箱の高さは段ごとにそろえる(段の上下がそろっていると矢印が読みやすい)
    def box_h(b):
        h = _BAND_H + _PAD + len(b["label"]) * _LABEL_LH
        if b["note"]:
            h += 2 + len(b["note"]) * _NOTE_LH
        if b["returns"]:
            h += 7 + len(b["returns"]) * _NOTE_LH
        return h + _PAD

    n_rows = max(rows) + 1
    row_h = [max([box_h(boxes[i["id"]]) for i in rows.get(r, []) if i["id"] in boxes] or [0])
             for r in range(n_rows)]
    widths = [sum(i["w"] for i in rows.get(r, [])) + _COL_GAP * max(len(rows.get(r, [])) - 1, 0)
              for r in range(n_rows)]
    content_w = max(widths)

    x_center, y = {}, _MARGIN
    tops = []
    for r in range(n_rows):
        items = rows.get(r, [])
        if r > 0:
            def bary(i):
                ps = [x_center[p] for p in preds.get(i["id"], []) if p in x_center]
                return (sum(ps) / len(ps) if ps else float("inf"), i["order"])
            items.sort(key=bary)
        else:
            items.sort(key=lambda i: i["order"])
        x = _MARGIN + (content_w - widths[r]) / 2
        for i in items:
            x_center[i["id"]] = x + i["w"] / 2
            x += i["w"] + _COL_GAP
        tops.append(y)
        y += row_h[r] + _ROW_GAP

    out_boxes = []
    for n in nodes:
        b = boxes[n["id"]]
        r = rank[n["id"]]
        b.update(x=x_center[n["id"]] - _NODE_W / 2, y=tops[r], w=_NODE_W, h=row_h[r])
        # 中身は上詰め。戻り矢印の注記だけ箱の下端に寄せる
        ty = b["y"] + _BAND_H + _PAD + _LABEL_SIZE
        b["label_y"] = [ty + k * _LABEL_LH for k in range(len(b["label"]))]
        ny = ty + (len(b["label"]) - 1) * _LABEL_LH + 2 + _NOTE_LH
        b["note_y"] = [ny + k * _NOTE_LH for k in range(len(b["note"]))]
        ry = b["y"] + b["h"] - _PAD - (len(b["returns"]) - 1) * _NOTE_LH - 3
        b["returns_y"] = [ry + k * _NOTE_LH for k in range(len(b["returns"]))]
        b["returns_line_y"] = ry - _NOTE_LH + 2
        out_boxes.append(b)

    # 矢印の付け根は、同じ箱から出る/入る矢印が重ならないよう箱の幅に散らす
    def spread(box_id, others):
        center = x_center[box_id]
        if box_id not in boxes:
            return {o: center for o in others}
        ordered = sorted(others, key=lambda o: x_center[o[1]])
        span = _NODE_W * 0.7
        return {o: center - span / 2 + span * (k + 1) / (len(ordered) + 1) for k, o in enumerate(ordered)}

    # 各矢印の段ごとの区間 (始点側ID, 終点側ID)
    segs = [(k, a, b) for k, chain in enumerate(chains) for a, b in zip(chain, chain[1:])]
    starts, ends = {}, {}
    for k, a, b in segs:
        starts.setdefault(a, []).append((k, b))
        ends.setdefault(b, []).append((k, a))
    start_x = {}
    for a, lst in starts.items():
        for (k, b), x in spread(a, lst).items():
            start_x[(k, a)] = x
    end_x = {}
    for b, lst in ends.items():
        for (k, a), x in spread(b, lst).items():
            end_x[(k, b)] = x

    # 矢印のラベルは曲線上に置く。同じ箱から何本も出ると中点どうしが重なるので、
    # 曲線上の位置(t)をずらして、先に置いたラベルと重ならない所を探す
    placed = []

    def bezier(p0, p1, p2, p3, t):
        u = 1 - t
        return u ** 3 * p0 + 3 * u * u * t * p1 + 3 * u * t * t * p2 + t ** 3 * p3

    def overlap_area(x, y, w):
        return sum(max(0, (w + pw) / 2 + 2 - abs(x - px)) * max(0, 17 - abs(y - py))
                   for px, py, pw in placed)

    out_edges = []
    for k, chain in enumerate(chains):
        e = forward[k]
        label = str(e.get("label") or "")
        lw = _text_width(label, _EDGE_LABEL_SIZE) + 10
        parts, cands = [], []
        for j, (a, b) in enumerate(zip(chain, chain[1:])):
            ra = rank[e["from"]] + j
            x1, y1 = start_x[(k, a)], tops[ra] + row_h[ra]
            x2, y2 = end_x[(k, b)], tops[ra + 1]
            if a not in boxes:
                parts.append(f"L{x1:.1f},{y1:.1f}")  # 中継点の段は縦にまっすぐ通す
                # 中継点を縦に通る区間は周りが空いているので、ラベルの置き場所の候補にする
                cands.append((x1, tops[ra] + row_h[ra] / 2))
            else:
                parts.append(f"M{x1:.1f},{y1:.1f}")
            m = (y2 - y1) / 2
            cands += [(bezier(x1, x1, x2, x2, t), bezier(y1, y1 + m, y2 - m, y2, t))
                      for t in (0.5, 0.68, 0.32, 0.82)]
            parts.append(f"C{x1:.1f},{y1 + m:.1f} {x2:.1f},{y2 - m:.1f} {x2:.1f},{y2:.1f}")
        label_pos = (0, 0)
        if label:
            # 重ならない最初の候補。どこでも重なるなら重なりが一番小さい所
            label_pos = next((c for c in cands if overlap_area(c[0], c[1], lw) == 0),
                             min(cands, key=lambda c: overlap_area(c[0], c[1], lw)))
            placed.append((label_pos[0], label_pos[1], lw))
        out_edges.append({
            "d": " ".join(parts),
            "label": label,
            "lx": label_pos[0], "ly": label_pos[1],
            "lw": lw,
        })

    return {
        "width": content_w + _MARGIN * 2,
        "height": y - _ROW_GAP + _MARGIN,
        "boxes": out_boxes,
        "edges": out_edges,
        "label_size": _LABEL_SIZE, "note_size": _NOTE_SIZE, "edge_label_size": _EDGE_LABEL_SIZE,
        "band_h": _BAND_H,
    }
