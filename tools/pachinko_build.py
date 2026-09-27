#!/usr/bin/env python3
"""
パチンコの機種データ(pachinko_data/*.json)から、機種ごとの単体HTMLページを書き出す
(店舗の store_build.py と同じ考え方)。

    python3 tools/pachinko_build.py              # 全機種 → pachinko_pages/
    python3 tools/pachinko_build.py eva17        # 指定した機種だけ(一覧は全機種で作り直す)

【なぜFlaskとは別にHTMLを書き出すのか】
サーバー無しでそのまま開け、共有もできるようにするため。ホールで開けば回転率の計算もできる
(各ページに、その機種のボーダーだけを使う計算を入れている)。
正はあくまでJSONで、HTMLは毎回作り直す生成物(手で直しても次の生成で消える)。
"""

import argparse
import os
import sys
from datetime import datetime

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pachinko_info  # noqa: E402

TEMPLATE_DIR = os.path.join(ROOT, "tools", "pachinko_page")
# 見た目は店舗ページと揃えたいので、CSSは店舗ページのものを土台に使い、足りない分だけ足す
BASE_CSS = os.path.join(ROOT, "tools", "store_page", "style.css")
OUT_DIR = os.path.join(ROOT, "pachinko_pages")


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
    border_points = [{"balls": balls, "value": machine[key]} for key, balls in pachinko_info.BORDER_POINTS
                     if machine[key]]
    html = env.get_template("page.html").render(m=machine, border_points=border_points,
                                                generated_at=generated_at)
    path = os.path.join(OUT_DIR, f"{machine['machine_id']}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


def build_index(env, machines, generated_at):
    order = {t: i for i, t in enumerate(pachinko_info.SPEC_TYPES)}
    machines = sorted(machines, key=lambda m: (order.get(m["spec_type"], len(order)), m["name"]))
    html = env.get_template("index.html").render(machines=machines, generated_at=generated_at)
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


def main():
    p = argparse.ArgumentParser(description="pachinko_data/*.json から機種ページのHTMLを生成する")
    p.add_argument("machine_ids", nargs="*", help="省略時は全機種")
    args = p.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    env = _env()
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    for mid in args.machine_ids or pachinko_info.machine_ids():
        machine = pachinko_info.load(mid)
        if machine is None:
            raise SystemExit(f"pachinko_data/{mid}.json がありません。")
        print(f"生成: {os.path.relpath(build_machine(env, machine, generated_at), ROOT)}")

    # 一覧は1機種だけ作り直したときも全機種から作る(一覧から機種が消えないように)
    machines, errors = pachinko_info.load_all()
    build_index(env, machines, generated_at)
    print(f"生成: pachinko_pages/index.html（{len(machines)} 機種）")
    for e in errors:
        print(f"  ⚠ {e}")


if __name__ == "__main__":
    main()
