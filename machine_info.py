"""
機種情報(解析まとめ)のJSONを読む層。

【なぜJSONファイルなのか】
機種情報は「新台が出るたびに1ファイル足す」運用で、AIに資料を渡して
machine_data/<id>.json を作らせる前提にしている。ファイルならAIの出力をそのまま置けて、
差分もgitで追える。画面(/info)はこのJSONを毎回読んで描くだけなので、
ファイルを置けば再起動なしでページが増える。

【判別DB(judge_db.py)との関係】
JSONは出典付きの「資料」で、判別に使う数値の正はあくまでSQLite側。
JSONの setting_estimation を判別DBの形に変換して取り込む(to_judge_spec)が、
自動では同期しない。判別スペック管理で手直しした値を、起動のたびに上書きしてしまうため。

【天井・期待値との関係】
JSONにも天井などを書くが、これは出典と一緒に読むための表示用。
期待値計算に使う天井スペックはシート側(machines)が正で、ここからは流し込まない。
"""

import json
import os

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
# 判別DBの形への変換
# ---------------------------------------------------------------------------
# 判別DBは設定1〜6の6列が必ず埋まっている前提(NOT NULL)。
# 設定1が無い機種などは、非搭載の設定を available_settings で候補から外したうえで、
# 列を埋めるために「搭載されている中で最も低い設定」の値を入れる。
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
    機種JSONを judge_db.upsert_machine に渡せる形に変換する。
    変換できなかった要素は黙って捨てず、skipped に理由を入れて返す。
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
