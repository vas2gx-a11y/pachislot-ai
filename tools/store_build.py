#!/usr/bin/env python3
"""
店舗情報JSON(store_data/*.json)から、店舗ごとの単体HTMLページを書き出す。

    python3 tools/store_build.py                 # 全店舗 → store_pages/
    python3 tools/store_build.py bellagio_nishinakajima
    python3 tools/store_build.py --with-sheet    # 営業データ(シート)も読み込んで載せる

【なぜFlaskとは別にHTMLを書き出すのか】
店舗情報はチャットで依頼して集め、HTMLとして貯めていく運用にしたため。
書き出したHTMLはサーバー無しでそのまま開け、共有もできる。
正はあくまでJSONで、HTMLは毎回JSONから作り直す(HTMLを手で直しても次の生成で消える)。

営業データ(日別・台別)はシートが正。--with-sheet のときだけ読む
(シートの認証情報が無い環境でも基本情報のページは作れるように)。
"""

import argparse
import os
import sys
from datetime import datetime

from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import store_info  # noqa: E402

TEMPLATE_DIR = os.path.join(ROOT, "tools", "store_page")
OUT_DIR = os.path.join(ROOT, "store_pages")


def _sort_by(rows, key):
    """新しい順・多い順。値が不明(None)の行はJinjaのsortで比較できず落ちるので末尾に回す。"""
    return sorted(rows or [], key=lambda r: (r.get(key) is not None, r.get(key) or 0), reverse=True)


def _operations(store, with_sheet):
    """営業データの概要。シートを読まないときは None(ページ側で「未読み込み」と出す)。"""
    if not with_sheet:
        return None
    # 画面(/store_info)と同じ集計を使う。common はシートの環境変数が必須なので、ここで初めて読む
    from routes.store_info import _operation_summary
    return _operation_summary(store.get("sheet_store_name") or store["name"])


def _env():
    env = Environment(loader=FileSystemLoader(TEMPLATE_DIR),
                      autoescape=select_autoescape(["html"]),
                      trim_blocks=True, lstrip_blocks=True)
    # CSSは各HTMLに埋め込む。1ファイルだけ渡したり開いたりしても見た目が崩れないように
    with open(os.path.join(TEMPLATE_DIR, "style.css"), encoding="utf-8") as f:
        css = f.read()
    env.globals.update(sort_by=_sort_by, field_labels=store_info.FIELD_LABELS, css=css)
    return env


def build_store(env, store, with_sheet, generated_at):
    sid = store["id"]
    info = store.get("info") or {}
    html = env.get_template("page.html").render(
        s=store,
        info=info,
        problems=store_info.validate(store, sid),
        source_no={src["id"]: i for i, src in enumerate(store.get("sources", []), start=1)},
        known=sum(1 for k in store_info.FIELD_KEYS if (info.get(k) or {}).get("status") != "不明"),
        field_count=len(store_info.FIELD_KEYS),
        ops=_operations(store, with_sheet),
        history=list(reversed(store.get("history", []))),
        generated_at=generated_at,
    )
    path = os.path.join(OUT_DIR, f"{sid}.html")
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    return path


def build_index(env, stores, generated_at):
    html = env.get_template("index.html").render(stores=stores, generated_at=generated_at)
    with open(os.path.join(OUT_DIR, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)


def main():
    p = argparse.ArgumentParser(description="store_data/*.json から店舗ページのHTMLを生成する")
    p.add_argument("store_ids", nargs="*", help="省略時は全店舗")
    p.add_argument("--with-sheet", action="store_true", help="営業データをシートから読んで載せる")
    args = p.parse_args()

    os.makedirs(OUT_DIR, exist_ok=True)
    env = _env()
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    for sid in args.store_ids or store_info.store_ids():
        store = store_info.load(sid)
        if store is None:
            raise SystemExit(f"store_data/{sid}.json がありません。")
        print(f"生成: {os.path.relpath(build_store(env, store, args.with_sheet, generated_at), ROOT)}")
        for problem in store_info.validate(store, sid):
            print(f"  ⚠ {problem}")

    # 一覧は1店舗だけ作り直したときも全店舗から作る(一覧から店舗が消えないように)
    stores, errors = store_info.load_all()
    build_index(env, stores, generated_at)
    print(f"生成: store_pages/index.html（{len(stores)} 店舗）")
    for e in errors:
        print(f"  ⚠ {e}")


if __name__ == "__main__":
    main()
