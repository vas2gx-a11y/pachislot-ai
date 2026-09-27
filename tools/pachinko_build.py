#!/usr/bin/env python3
"""
パチンコの機種データから、機種ごとの単体HTMLページを書き出す(店舗の store_build.py と同じ考え方)。

    python3 tools/pachinko_build.py                  # data/pachinko_collected/*.json → pachinko_pages/
    python3 tools/pachinko_build.py data/pachinko_collected/eva17.json
    python3 tools/pachinko_build.py --from-sheet     # シート(pachinko_machines)の中身から作る

【なぜFlaskとは別にHTMLを書き出すのか】
サーバー無しでそのまま開け、共有もできるようにするため。ホールで開けば回転率の計算もできる
(各ページに、その機種のボーダーだけを使う計算を入れている)。
HTMLは毎回作り直す生成物なので、手で直しても次の生成で消える。

既定の読み込み元はチャットで調べた結果のJSON(tools/pachinko_apply.py でシートに入れるもの)。
シートの認証情報が無い環境でも作れるようにするため。画面から直した内容まで載せたいときは --from-sheet。
"""

import argparse
import glob
import json
import os
import sys
from datetime import datetime

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TEMPLATE_DIR = os.path.join(ROOT, "tools", "pachinko_page")
# 見た目は店舗ページと揃えたいので、CSSは店舗ページのものを土台に使い、足りない分だけ足す
BASE_CSS = os.path.join(ROOT, "tools", "store_page", "style.css")
OUT_DIR = os.path.join(ROOT, "pachinko_pages")
DEFAULT_GLOB = os.path.join(ROOT, "data", "pachinko_collected", "*.json")

# 一覧の並び。アプリの PACHINKO_SPEC_TYPES と同じ順(common を読むとシートの環境変数が要るので写している)
SPEC_ORDER = ["ミドル", "ライトミドル", "甘デジ", "ライト", "スマパチ", "その他"]
# ボーダーの換算点(common.PACHINKO_BORDER_POINTS と同じ。1,000円分を何玉で交換するか)
BORDER_POINTS = [("border_equiv", 250), ("border_28", 280), ("border_33", 330)]


def _from_json(paths):
    machines = []
    for path in paths:
        with open(path, encoding="utf-8") as f:
            machine = json.load(f)
        # ファイル名をページ名に使う(機種名は記号や空白が多く、ファイル名に向かないため)
        machine["page_id"] = os.path.splitext(os.path.basename(path))[0]
        machines.append(machine)
    return machines


def _from_sheet():
    import common
    return [{**m, "page_id": m["machine_id"]} for m in common.load_pachinko_machines()]


def _fmt(value):
    """319.7 → '319.7'、17.0 → '17'(末尾の .0 を出さない)。None は None のまま。"""
    return None if value is None else f"{value:g}"


def _env():
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR),
                      autoescape=select_autoescape(["html"]),
                      trim_blocks=True, lstrip_blocks=True)
    # CSSは各HTMLに埋め込む。1ファイルだけ渡したり開いたりしても見た目が崩れないように
    css = ""
    for path in (BASE_CSS, os.path.join(TEMPLATE_DIR, "style.css")):
        with open(path, encoding="utf-8") as f:
            css += f.read() + "\n"
    env.globals.update(css=css, fmt=_fmt)
    return env


def build_machine(env, machine, generated_at):
    border_points = [{"balls": balls, "value": machine.get(key)} for key, balls in BORDER_POINTS
                     if machine.get(key)]
    html = env.get_template("page.html").render(m=machine, border_points=border_points,
                                                generated_at=generated_at)
    path = os.path.join(OUT_DIR, f"{machine['page_id']}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


def build_index(env, machines, generated_at):
    order = {t: i for i, t in enumerate(SPEC_ORDER)}
    machines = sorted(machines, key=lambda m: (order.get(m.get("spec_type"), len(order)), m["name"]))
    html = env.get_template("index.html").render(machines=machines, generated_at=generated_at)
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


def main():
    p = argparse.ArgumentParser(description="パチンコの機種データから単体HTMLを生成する")
    p.add_argument("paths", nargs="*", help="機種JSON。省略時は data/pachinko_collected/*.json")
    p.add_argument("--from-sheet", action="store_true", help="JSONではなくシートの中身から作る")
    args = p.parse_args()

    if args.from_sheet:
        machines = _from_sheet()
    else:
        paths = args.paths or sorted(glob.glob(DEFAULT_GLOB))
        if not paths:
            raise SystemExit("機種JSONがありません(data/pachinko_collected/*.json)。")
        machines = _from_json(paths)

    os.makedirs(OUT_DIR, exist_ok=True)
    env = _env()
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    for machine in machines:
        print(f"生成: {os.path.relpath(build_machine(env, machine, generated_at), ROOT)}")

    # 一覧は一部の機種だけ作り直したときも、調べた全機種のJSONから作る(一覧から機種が消えないように)
    if not args.from_sheet and args.paths:
        machines = _from_json(sorted(glob.glob(DEFAULT_GLOB)))
    build_index(env, machines, generated_at)
    print(f"生成: pachinko_pages/index.html（{len(machines)} 機種）")


if __name__ == "__main__":
    main()
