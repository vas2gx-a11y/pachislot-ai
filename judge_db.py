"""
設定判別に使う機種スペックのSQLite層。

【なぜここだけシートではなくSQLiteなのか】
判別スペックは「1機種 : 複数の判別要素 : さらに複数の選択肢」という入れ子の構造を持つ。
1行1機種のスプレッドシートではこの形を素直に置けず、JSON文字列を1セルに詰め込む形になり、
検証も部分更新も効かなくなる。判別は数値の正しさがそのまま結果に出る部分なので、
ここだけリレーションを持てるSQLiteをマスターにしている。
実戦記録・店舗データ・ゲームフローは従来どおり common.py(シート)のままで、こちらとは独立。

【天井スペックを持たない理由】
天井・期待値の計算は既にシート側(機種スペック + 期待値計算)にある。
同じ項目を2か所に置くとどちらが正か分からなくなるため、このDBは
「設定判別に必要なもの」= 判別要素と機械割だけに絞っている。

【デプロイ時の注意】
Renderの無料プランはディスクが揮発性で、再デプロイのたびにこのDBは消える。
残したい場合は永続ディスクを用意して JUDGE_DB_PATH をそのパスに向ける。
"""

import os
import sqlite3

from flask import g

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("JUDGE_DB_PATH", os.path.join(BASE_DIR, "data", "judge.db"))

SCHEMA = """
-- 機種の基本情報。判別に必要な項目だけを持つ(天井などはシート側の管轄)。
CREATE TABLE IF NOT EXISTS judge_machines (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    maker       TEXT,

    -- 機械割(設定1〜6 / %)。判別結果から設定別の実質時給を出すのに使う。
    payout1     REAL,
    payout2     REAL,
    payout3     REAL,
    payout4     REAL,
    payout5     REAL,
    payout6     REAL,

    created_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_judge_machines_name ON judge_machines(name);

-- 判別要素(小役・ボーナスなど)。1機種が複数持てるよう機種本体から分離している。
-- 確率は分母で保持する(1/6.35 なら 6.35)。
CREATE TABLE IF NOT EXISTS judge_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id  INTEGER NOT NULL REFERENCES judge_machines(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,
    -- koyaku: 毎G試行するカウント小役 / bonus: 初当り・ボーナス
    -- ratio : 母数が総回転数でないもの(CZ成功率など)。分母は「試行あたり」で持つ
    item_type   TEXT    NOT NULL DEFAULT 'koyaku',

    -- 確率を割る母数の種類。
    -- total : 総回転数 / normal: 通常時のみ(AT・上位AT消化分を除く) / custom: 試行回数を個別入力
    -- AT中も回る機種では、通常時にしか成立しない要素を総回転数で割ると確率が薄まり、
    -- 設定を過小評価する。そのため要素ごとに母数を選べるようにしている。
    denom_base  TEXT    NOT NULL DEFAULT 'total',
    denom1      REAL    NOT NULL,
    denom2      REAL    NOT NULL,
    denom3      REAL    NOT NULL,
    denom4      REAL    NOT NULL,
    denom5      REAL    NOT NULL,
    denom6      REAL    NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_judge_items_machine ON judge_items(machine_id);

-- 終了画面・トロフィーのように「複数の選択肢のうちどれが出たか」で判別する要素。
-- 1回の観測が持つ情報量が大きく、数回の観測で設定を絞り込める。
CREATE TABLE IF NOT EXISTS categorical_groups (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id  INTEGER NOT NULL REFERENCES judge_machines(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,   -- 例: AT終了画面
    sort_order  INTEGER NOT NULL DEFAULT 0
);

-- 各選択肢の設定別出現率。合計が1にならなくてもよい(計算時に正規化する)。
CREATE TABLE IF NOT EXISTS categorical_options (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id    INTEGER NOT NULL REFERENCES categorical_groups(id) ON DELETE CASCADE,
    name        TEXT    NOT NULL,   -- 例: カサンドラ
    p1          REAL    NOT NULL,
    p2          REAL    NOT NULL,
    p3          REAL    NOT NULL,
    p4          REAL    NOT NULL,
    p5          REAL    NOT NULL,
    p6          REAL    NOT NULL,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_cat_groups_machine ON categorical_groups(machine_id);
CREATE INDEX IF NOT EXISTS idx_cat_options_group ON categorical_options(group_id);

-- 機種固有の確定・否定演出(トロフィー、終了画面のスタンプなど)。
-- 出た瞬間に該当しない設定を候補から外すため、判別要素の中で最も強い。
CREATE TABLE IF NOT EXISTS confirmation_items (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    machine_id  INTEGER NOT NULL REFERENCES judge_machines(id) ON DELETE CASCADE,
    group_name  TEXT    NOT NULL,   -- 例: ST終了画面（スタンプ）
    name        TEXT    NOT NULL,   -- 例: 極
    s1          INTEGER NOT NULL DEFAULT 1,   -- 1: その設定はあり得る / 0: 否定される
    s2          INTEGER NOT NULL DEFAULT 1,
    s3          INTEGER NOT NULL DEFAULT 1,
    s4          INTEGER NOT NULL DEFAULT 1,
    s5          INTEGER NOT NULL DEFAULT 1,
    s6          INTEGER NOT NULL DEFAULT 1,
    sort_order  INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_confirm_machine ON confirmation_items(machine_id);
"""

# 後から追加したカラム。起動時に不足分だけALTERする。
# (SQLiteは ADD COLUMN しかできないため、削除・型変更を伴う変更はここでは扱わない)
ADDED_COLUMNS = [
    # 搭載されている設定を "011111" のように1〜6の順で持つ(0: 非搭載)。
    # 設定1が無い機種を6段階のまま扱うと、存在しない設定1に事後確率が残ってしまうため、
    # 判別時は非搭載の設定を最初から候補から外す。
    ("judge_machines", "available_settings", "TEXT NOT NULL DEFAULT '111111'"),
]


def get_db():
    """リクエスト内で使い回すDB接続を返す。"""
    if "judge_db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        # 機種を消したら判別要素も消えるように、接続ごとに外部キーを有効化する
        # (SQLiteの既定はOFF。接続単位の設定なのでここで毎回入れる必要がある)
        conn.execute("PRAGMA foreign_keys = ON")
        g.judge_db = conn
    return g.judge_db


def close_db(exception=None):
    db = g.pop("judge_db", None)
    if db is not None:
        db.close()


def init_db():
    """初回起動時にテーブルを作成し、既存DBのスキーマ差分を埋める。"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

    db = get_db()
    db.executescript(SCHEMA)

    for table, column, coltype in ADDED_COLUMNS:
        existing = {row["name"] for row in db.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

    db.commit()


def query_db(query, args=(), one=False):
    cur = get_db().execute(query, args)
    rows = cur.fetchall()
    cur.close()
    return (rows[0] if rows else None) if one else rows


def execute_db(query, args=()):
    db = get_db()
    cur = db.execute(query, args)
    db.commit()
    lastrowid = cur.lastrowid
    cur.close()
    return lastrowid


# ---------------------------------------------------------------------------
# 機種スペックの読み出し
# ---------------------------------------------------------------------------
# 判別ページとJSON APIの両方から同じ形を使うため、組み立てはここに1本化する。
# (画面ごとに別々に組み立てると、片方だけ直して食い違うため)

def load_machines_for_client():
    """判別エンジンがそのまま使える形で全機種を返す。"""
    machines = query_db("SELECT * FROM judge_machines ORDER BY name")
    judge_rows = query_db("SELECT * FROM judge_items ORDER BY machine_id, sort_order, id")
    group_rows = query_db("SELECT * FROM categorical_groups ORDER BY machine_id, sort_order, id")
    option_rows = query_db("SELECT * FROM categorical_options ORDER BY group_id, sort_order, id")
    confirm_rows = query_db("SELECT * FROM confirmation_items ORDER BY machine_id, sort_order, id")

    items_by_machine = {}
    for j in judge_rows:
        items_by_machine.setdefault(j["machine_id"], []).append({
            "name": j["name"],
            "type": j["item_type"],
            "denomBase": j["denom_base"],
            "denominators": [j[f"denom{i}"] for i in range(1, 7)],
        })

    options_by_group = {}
    for o in option_rows:
        options_by_group.setdefault(o["group_id"], []).append({
            "name": o["name"],
            "probabilities": [o[f"p{i}"] for i in range(1, 7)],
        })

    groups_by_machine = {}
    for grp in group_rows:
        options = options_by_group.get(grp["id"], [])
        if len(options) < 2:
            continue  # 選択肢が1つでは判別に使えない
        groups_by_machine.setdefault(grp["machine_id"], []).append({
            "name": grp["name"],
            "options": options,
        })

    confirms_by_machine = {}
    for c in confirm_rows:
        confirms_by_machine.setdefault(c["machine_id"], []).append({
            "group": c["group_name"],
            "name": c["name"],
            "flags": [bool(c[f"s{i}"]) for i in range(1, 7)],
        })

    result = []
    for m in machines:
        payouts = [m[f"payout{i}"] for i in range(1, 7)]
        available = (m["available_settings"] or "111111").ljust(6, "1")[:6]
        result.append({
            "id": m["id"],
            "name": m["name"],
            "maker": m["maker"],
            "availableSettings": [c == "1" for c in available],
            # 1つでも欠けていると時給計算が破綻するので、全部揃っているときだけ渡す
            "payouts": payouts if all(p is not None for p in payouts) else None,
            "judgeItems": items_by_machine.get(m["id"], []),
            "categoricalGroups": groups_by_machine.get(m["id"], []),
            "confirmations": confirms_by_machine.get(m["id"], []),
        })
    return result


# ---------------------------------------------------------------------------
# 判別スペックの書き込み
# ---------------------------------------------------------------------------
# 登録フォーム(routes/judge.py)と機種情報JSONからの取り込み(routes/machine_info.py)の
# 両方が同じ保存処理を通るよう、ここに置いている。

def save_children(machine_id, data):
    """判別要素・選択肢・確定演出を差分更新せず、一度消してから入れ直す。
    行の増減と並べ替えが自由なフォームなので、差分を取るより入れ直す方が確実。
    """
    db = get_db()

    db.execute("DELETE FROM judge_items WHERE machine_id = ?", (machine_id,))
    for order, item in enumerate(data["judge_items"]):
        db.execute(
            """INSERT INTO judge_items
               (machine_id, name, item_type, denom_base,
                denom1, denom2, denom3, denom4, denom5, denom6, sort_order)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (machine_id, item["name"], item["item_type"], item["denom_base"],
             *item["denominators"], order),
        )

    for row in db.execute(
        "SELECT id FROM categorical_groups WHERE machine_id = ?", (machine_id,)
    ).fetchall():
        db.execute("DELETE FROM categorical_options WHERE group_id = ?", (row["id"],))
    db.execute("DELETE FROM categorical_groups WHERE machine_id = ?", (machine_id,))
    for gi, group in enumerate(data["categorical_groups"]):
        cur = db.execute(
            "INSERT INTO categorical_groups (machine_id, name, sort_order) VALUES (?,?,?)",
            (machine_id, group["name"], gi),
        )
        group_id = cur.lastrowid
        for oi, opt in enumerate(group["options"]):
            db.execute(
                """INSERT INTO categorical_options
                   (group_id, name, p1, p2, p3, p4, p5, p6, sort_order)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (group_id, opt["name"], *opt["probabilities"], oi),
            )

    db.execute("DELETE FROM confirmation_items WHERE machine_id = ?", (machine_id,))
    for order, item in enumerate(data["confirmations"]):
        db.execute(
            """INSERT INTO confirmation_items
               (machine_id, group_name, name, s1, s2, s3, s4, s5, s6, sort_order)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (machine_id, item["group"], item["name"], *item["flags"], order),
        )

    db.commit()


def upsert_machine(data):
    """
    機種名が同じなら上書き、無ければ新規登録する(取り込みのやり直しが効くように)。
    戻り値は (machine_id, 新規かどうか)。
    """
    existing = query_db("SELECT id FROM judge_machines WHERE name = ?", (data["name"],), one=True)
    if existing:
        machine_id = existing["id"]
        execute_db(
            """UPDATE judge_machines SET
                 maker=?, payout1=?, payout2=?, payout3=?, payout4=?, payout5=?, payout6=?,
                 available_settings=?, updated_at=datetime('now','localtime')
               WHERE id=?""",
            (data["maker"], *data["payouts"], data["available_settings"], machine_id),
        )
        created = False
    else:
        machine_id = execute_db(
            """INSERT INTO judge_machines
               (name, maker, payout1, payout2, payout3, payout4, payout5, payout6, available_settings)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (data["name"], data["maker"], *data["payouts"], data["available_settings"]),
        )
        created = True

    save_children(machine_id, data)
    return machine_id, created
