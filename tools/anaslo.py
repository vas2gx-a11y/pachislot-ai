#!/usr/bin/env python3
"""
アナスロ(ana-slo.com)の日別ホールデータをCSV化・集計するツール。

日別ページ(例: https://ana-slo.com/2026-08-22-楽園大宮店-data/)のHTMLには
<table id="all_data_table"> という全台データの表がそのまま埋め込まれている。
    機種名 / 台番号 / G数 / 差枚 / BB / RB / ART / 合成確率 / BB確率 / RB確率 / ART確率
このツールはその表だけを取り出してCSVにし、機種別・台番別・曜日別などに集計する。

使い方:
    # 保存したHTML(またはブラウザ側スクリプトが出したHTML)をCSV化
    python3 tools/anaslo.py parse ~/Downloads/*.html --out data/anaslo_楽園大宮店.csv

    # 直接URLから取れるか試す(Cloudflareに弾かれる場合はブラウザ側スクリプトを使う)
    python3 tools/anaslo.py parse --url https://ana-slo.com/2026-08-22-楽園大宮店-data/ \
        --out data/anaslo_楽園大宮店.csv --append

    # 集計(機種別・直近30日)
    python3 tools/anaslo.py agg data/anaslo_楽園大宮店.csv --by machine --days 30

    # 集計(台番別・ゾロ目日のみ・5サンプル以上)
    python3 tools/anaslo.py agg data/anaslo_楽園大宮店.csv --by number --zorome --min-samples 5

依存ライブラリなし(標準ライブラリのみ)。
"""

import argparse
import csv
import glob
import html
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from urllib.parse import unquote

BASE_URL = "https://ana-slo.com"

# 出力CSVの列。日付+店舗+台番号で1行が一意になる。
CSV_FIELDS = [
    "date", "store_name", "machine_number", "machine_name",
    "games", "diff", "bb", "rb", "art",
    "total_rate", "bb_rate", "rb_rate", "art_rate",
]

# 全台データの表。id が固定なのでこれを目印にする。
_ALL_DATA_TABLE_RE = re.compile(r'<table[^>]*id="all_data_table".*?</table>', re.S)
_ROW_RE = re.compile(r"<tr>(.*?)</tr>", re.S)
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
_TAG_RE = re.compile(r"<[^>]+>")

# 店舗の「データ一覧」ページ(日付・総差枚・平均差枚・平均G数・勝率の一覧)
_LIST_STORE_RE = re.compile(r"<title>\s*(.+?)\s+データ一覧")
_LIST_CELL_RE = re.compile(r'<div class="table-data-cell">(.*?)</div>', re.S)
_LIST_DATE_RE = re.compile(r"(\d{4})/(\d{1,2})/(\d{1,2})")
# データなしを表す記号(アプリ側 common.py の _HALL_EMPTY_TOKENS と揃えている)
EMPTY_TOKENS = {"-", "–", "—", "―", "ー", "‐", "−", "?", "？", "N/A", "n/a", ""}

# 日付と店名の取得元(どれか1つ当たればよい)
_TITLE_RE = re.compile(r"<title>\s*(\d{4})/(\d{1,2})/(\d{1,2})\s+(.+?)\s+データまとめ")
_CANONICAL_RE = re.compile(
    r'rel="canonical"[^>]*href="[^"]*?/(\d{4})-(\d{2})-(\d{2})-(.+?)-data/"'
)
_FILENAME_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})-(.*?)(?:-data)?(?:\.html?)?$")

WEEKDAY_LABELS = ["月", "火", "水", "木", "金", "土", "日"]


# ---------------------------------------------------------------------------
# HTML解析
# ---------------------------------------------------------------------------
def _text(cell_html):
    """セルのHTMLから中身のテキストだけを取り出す"""
    return html.unescape(_TAG_RE.sub("", cell_html)).replace(" ", " ").strip()


def parse_int(value):
    """'7,482' '-1,571' '+191' → int。数値として読めなければ None"""
    if value is None:
        return None
    cleaned = str(value).replace(",", "").replace("+", "").strip()
    if not cleaned or cleaned in {"-", "―"}:
        return None
    try:
        return int(float(cleaned))
    except ValueError:
        return None


def parse_rate(value):
    """
    '1/152.7' → 152.7(確率の分母)を返す。
    当たりが0回のとき '1/0.0' と表示されるが、これは「確率0」ではなく
    「母数不足で算出できない」なので None にしておく(平均を歪ませないため)。
    """
    if not value:
        return None
    match = re.search(r"1\s*/\s*([\d,.]+)", str(value))
    if not match:
        return None
    try:
        denominator = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return denominator if denominator > 0 else None


def extract_meta(html_text, source_name=""):
    """HTML(とファイル名)から対象日と店舗名を取り出す"""
    match = _TITLE_RE.search(html_text)
    if match:
        year, month, day, store = match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}", store.strip()

    match = _CANONICAL_RE.search(html_text)
    if match:
        year, month, day, store = match.groups()
        return f"{year}-{month}-{day}", unquote(store)

    base = os.path.basename(source_name)
    base = re.sub(r"\.html?$", "", base, flags=re.I)
    match = _FILENAME_RE.search(unquote(base))
    if match:
        year, month, day, store = match.groups()
        return f"{year}-{month}-{day}", store.strip("-")

    return "", ""


def parse_day_html(html_text, store_name="", target_date="", source_name=""):
    """
    日別ページのHTMLから全台データを取り出して、行(dict)のリストで返す。
    store_name / target_date を明示した場合はそちらを優先する。
    """
    if "Just a moment" in html_text[:2000] or "cf-browser-verification" in html_text[:5000]:
        raise ValueError(
            "Cloudflareの確認ページが保存されています。"
            "ブラウザで実際にデータが表示された状態のHTMLを使ってください。"
        )

    meta_date, meta_store = extract_meta(html_text, source_name)
    day = target_date or meta_date
    store = store_name or meta_store

    table_match = _ALL_DATA_TABLE_RE.search(html_text)
    if not table_match:
        raise ValueError("全台データの表(all_data_table)が見つかりませんでした。")

    rows = []
    for row_html in _ROW_RE.findall(table_match.group(0)):
        cells = [_text(c) for c in _CELL_RE.findall(row_html)]
        # 見出し行(機種名/台番号...)や列数の足りない行は捨てる
        if len(cells) < 11 or cells[0] == "機種名":
            continue
        machine_number = parse_int(cells[1])
        if machine_number is None:
            continue
        rows.append({
            "date": day,
            "store_name": store,
            "machine_number": machine_number,
            "machine_name": cells[0],
            "games": parse_int(cells[2]),
            "diff": parse_int(cells[3]),
            "bb": parse_int(cells[4]),
            "rb": parse_int(cells[5]),
            "art": parse_int(cells[6]),
            "total_rate": parse_rate(cells[7]),
            "bb_rate": parse_rate(cells[8]),
            "rb_rate": parse_rate(cells[9]),
            "art_rate": parse_rate(cells[10]),
        })

    if not rows:
        raise ValueError("表は見つかりましたが、データ行が0件でした。")
    return rows


def parse_list_html(html_text, store_name=""):
    """
    店舗の「データ一覧」ページ(例: 楽園大宮店 データ一覧)のHTMLから、
    日付ごとの 総差枚 / 平均差枚 / 平均G数 / 勝率 を取り出す。

    このページは表示上の値をそのまま持っているだけなので、アナスロ側が「–」にしている
    欄はこちらでも取れない(実際、総差枚・平均差枚・勝率はごく一部の日にしか入っていない)。
    値は文字列のまま返し、埋まっているかどうかの判断は呼び出し側に任せる。
    """
    store = store_name
    if not store:
        matched = _LIST_STORE_RE.search(html_text)
        store = matched.group(1).strip() if matched else ""

    rows = []
    for chunk in html_text.split('<div class="table-row"')[1:]:
        cells = [_text(c) for c in _LIST_CELL_RE.findall(chunk)]
        if len(cells) < 5:
            continue  # 見出し行(table-header-cell)はここで落ちる
        matched = _LIST_DATE_RE.search(cells[0])
        if not matched:
            continue
        year, month, day = (int(g) for g in matched.groups())
        try:
            date_str = date(year, month, day).isoformat()
        except ValueError:
            continue
        rows.append({
            "date": date_str,
            "store_name": store,
            "total_diff": cells[1],
            "avg_diff": cells[2],
            "avg_games": cells[3],
            "win_rate": cells[4],
        })

    # 保存したページには同じ表がPC用・スマホ用で二重に入っていることがあるため、
    # 日付で名寄せする(値が入っている方を優先して残す)
    by_date = {}
    for row in rows:
        existing = by_date.get(row["date"])
        if existing is None or (not _is_filled(existing["total_diff"]) and _is_filled(row["total_diff"])):
            by_date[row["date"]] = row

    return sorted(by_date.values(), key=lambda r: r["date"], reverse=True), store


def _is_filled(value):
    return str(value).strip() not in EMPTY_TOKENS


def daily_rows_from_units(rows):
    """
    日別ページ由来の台データ(parseで作ったCSV)から、日ごとの店舗全体の値を作る。

    アナスロが一覧ページに出している「平均G数」と同じ定義(全台の単純平均)で計算しているので、
    一覧側に「–」で欠けている 総差枚・平均差枚・勝率 をこちらで埋められる。
    """
    by_date = {}
    for row in rows:
        by_date.setdefault(row.get("date"), []).append(row)

    daily = []
    for date_str, members in by_date.items():
        diffs = [r["diff"] for r in members if r.get("diff") is not None]
        games = [r["games"] for r in members if r.get("games") is not None]
        if not diffs or not games:
            continue
        wins = sum(1 for d in diffs if d > 0)
        daily.append({
            "date": date_str,
            "store_name": members[0].get("store_name", ""),
            "total_diff": f"{sum(diffs):+,}",
            "avg_diff": f"{round(sum(diffs) / len(diffs)):+,}",
            "avg_games": f"{round(sum(games) / len(games)):,}",
            "win_rate": f"{100 * wins / len(diffs):.1f}%({wins}/{len(diffs)})",
        })
    daily.sort(key=lambda r: r["date"], reverse=True)
    return daily


def to_unit_paste_text(rows, date_str=""):
    """
    台別データを、アプリの「台別データの取り込み」欄に貼り付けられる形にする。

    アプリ側の parse_hall_unit_text は
      ・見出し行(台番号 G数 差枚 BB RB)で列を判定し
      ・数値でない短い行を機種名の見出しとみなす
    という読み方をするので、機種ごとにまとめて 機種名 → その機種の台 の順に並べる。

    ART回数はアプリの台別データに列がないため出力しない(BB/RBのみ)。
    AT機は差枚とG数で見ることになる。
    """
    selected = [r for r in rows if not date_str or r.get("date") == date_str]
    if not selected:
        return "", {}

    by_machine = {}
    for row in sorted(selected, key=lambda r: r.get("machine_number") or 0):
        by_machine.setdefault(row.get("machine_name") or "不明", []).append(row)

    lines = ["台番号\tG数\t差枚\tBB\tRB"]
    for machine_name, members in by_machine.items():
        lines.append(machine_name)
        for row in members:
            lines.append("\t".join([
                str(row.get("machine_number") or ""),
                f"{row['games']:,}" if row.get("games") is not None else "-",
                f"{row['diff']:+,}" if row.get("diff") is not None else "-",
                str(row.get("bb") if row.get("bb") is not None else "-"),
                str(row.get("rb") if row.get("rb") is not None else "-"),
            ]))

    report = {"units": len(selected), "machines": len(by_machine),
              "date": date_str or selected[0].get("date", "")}
    return "\n".join(lines), report


def to_paste_text(rows):
    """
    アプリの店舗傾向ページ(日別データ取り込み)に貼り付けられる形のテキストにする。
    列は 日付 / 総差枚 / 平均差枚 / 平均G数 / 勝率 のタブ区切り。
    """
    lines = []
    for row in rows:
        day = _to_date(row["date"])
        label = f"{day.strftime('%Y/%m/%d')}({WEEKDAY_LABELS[day.weekday()]})"
        cells = [row.get(k) for k in ("total_diff", "avg_diff", "avg_games", "win_rate")]
        lines.append("\t".join([label] + [c if _is_filled(c) else "-" for c in cells]))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 取得(直アクセス。弾かれたらブラウザ側スクリプトへ誘導する)
# ---------------------------------------------------------------------------
def build_day_url(store_name, day):
    from urllib.parse import quote
    return f"{BASE_URL}/{day}-{quote(store_name)}-data/"


def fetch_html(url, timeout=30):
    """
    日別ページを普通のGETで取得する。
    ana-slo.com はCloudflareのbot対策が有効なため、環境によっては403が返る。
    その場合は tools/anaslo_fetch.js(ブラウザのコンソールで動かす版)を使う。
    """
    import urllib.error
    import urllib.request

    request = urllib.request.Request(url, headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/126.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "ja,en-US;q=0.9",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        if e.code in (403, 503):
            raise RuntimeError(
                f"{url} が {e.code} で拒否されました(Cloudflareのbot対策)。\n"
                "  → tools/anaslo_fetch.js をブラウザのコンソールで実行してCSVを作り、\n"
                "     このツールの agg サブコマンドで集計してください。"
            ) from e
        raise RuntimeError(f"{url} の取得に失敗しました: HTTP {e.code}") from e


# ---------------------------------------------------------------------------
# CSV入出力
# ---------------------------------------------------------------------------
def load_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return [_restore_types(row) for row in csv.DictReader(f)]


def _restore_types(row):
    for key in ("machine_number", "games", "diff", "bb", "rb", "art"):
        row[key] = parse_int(row.get(key))
    for key in ("total_rate", "bb_rate", "rb_rate", "art_rate"):
        value = (row.get(key) or "").strip()
        row[key] = float(value) if value else None
    return row


def write_csv(path, rows):
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    # Excelでそのまま開けるようBOM付きUTF-8で書く
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in CSV_FIELDS})


def dedupe(rows):
    """同じ 日付×店舗×台番号 は後から読んだ方で上書きする(再取得したデータを正とする)"""
    merged = {}
    for row in rows:
        merged[(row.get("date"), row.get("store_name"), row.get("machine_number"))] = row
    return sorted(
        merged.values(),
        key=lambda r: (r.get("date") or "", r.get("machine_number") or 0),
    )


# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------
def _to_date(value):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def is_zorome(day):
    """ゾロ目日(11日・22日)と、月日がゾロ目の日(1/1、8/8など)"""
    return day.day in (11, 22) or day.month == day.day


def filter_rows(rows, days=0, machine="", number=None, weekday=None,
                zorome=False, since="", until=""):
    since_date = _to_date(since)
    until_date = _to_date(until)
    cutoff = date.today() - timedelta(days=days - 1) if days else None

    selected = []
    for row in rows:
        day = _to_date(row.get("date"))
        if day is None:
            continue
        if cutoff and day < cutoff:
            continue
        if since_date and day < since_date:
            continue
        if until_date and day > until_date:
            continue
        if zorome and not is_zorome(day):
            continue
        if weekday is not None and day.weekday() != weekday:
            continue
        if machine and machine not in (row.get("machine_name") or ""):
            continue
        if number is not None and row.get("machine_number") != number:
            continue
        selected.append(row)
    return selected


GROUP_KEYS = {
    "machine": ("機種", lambda r: r.get("machine_name") or "不明"),
    "number": ("台番号", lambda r: str(r.get("machine_number"))),
    "date": ("日付", lambda r: r.get("date") or ""),
    "weekday": ("曜日", lambda r: WEEKDAY_LABELS[_to_date(r["date"]).weekday()]),
    "machine-number": ("機種+台番号",
                       lambda r: f"{r.get('machine_name')} #{r.get('machine_number')}"),
    # 店舗全体の 総差枚/平均差枚/平均G数/勝率 は、アプリの「店舗の年間データ」欄に
    # そのまま入力できる形になっている
    "store": ("店舗全体", lambda r: r.get("store_name") or "全体"),
}


def aggregate(rows, by="machine"):
    """グループごとに 台数 / 総差枚 / 平均差枚 / 平均G数 / 勝率 / 合成確率 を出す"""
    label, key_func = GROUP_KEYS[by]
    groups = {}
    for row in rows:
        groups.setdefault(key_func(row), []).append(row)

    results = []
    for name, members in groups.items():
        diffs = [r["diff"] for r in members if r.get("diff") is not None]
        games = [r["games"] for r in members if r.get("games") is not None]
        hits = sum((r.get("bb") or 0) + (r.get("rb") or 0) + (r.get("art") or 0)
                   for r in members)
        total_games = sum(games)
        wins = sum(1 for d in diffs if d > 0)
        results.append({
            "group": name,
            "samples": len(members),
            "total_diff": sum(diffs),
            "avg_diff": round(sum(diffs) / len(diffs)) if diffs else 0,
            "avg_games": round(total_games / len(games)) if games else 0,
            "win_rate": round(100 * wins / len(diffs), 1) if diffs else 0.0,
            "max_diff": max(diffs) if diffs else 0,
            "min_diff": min(diffs) if diffs else 0,
            # 平均の平均ではなく「総G数 ÷ 総当たり回数」で出す(台ごとの母数差を潰さないため)
            "total_rate": round(total_games / hits, 1) if hits else None,
        })
    return label, results


AGG_COLUMNS = [
    ("group", "{}", 28),
    ("samples", "{}", 6),
    ("total_diff", "{:+,}", 11),
    ("avg_diff", "{:+,}", 9),
    ("avg_games", "{:,}", 9),
    ("win_rate", "{}%", 7),
    ("max_diff", "{:+,}", 9),
    ("min_diff", "{:+,}", 9),
    ("total_rate", "1/{}", 9),
]
AGG_HEADERS = ["対象", "件数", "総差枚", "平均差枚", "平均G数", "勝率", "最高", "最低", "合成"]


def _display_width(text):
    """全角文字を2文字幅として数える(ターミナルで列を揃えるため)"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in text)


def _truncate(text, width):
    """表示幅がwidthを超える文字列は末尾を…に置き換える(列崩れ防止)"""
    if _display_width(text) <= width:
        return text
    clipped = ""
    for char in text:
        if _display_width(clipped + char) > width - 1:
            break
        clipped += char
    return clipped + "…"


def _pad(text, width):
    text = _truncate(text, width)
    return text + " " * max(0, width - _display_width(text))


def print_aggregate(label, results, limit=30):
    header = "  ".join(_pad(h, w) for h, (_, _, w) in zip(AGG_HEADERS, AGG_COLUMNS))
    print(f"\n[{label}別]  ※平均差枚の高い順")
    print(header)
    print("-" * _display_width(header))
    for result in results[:limit]:
        cells = []
        for key, fmt, width in AGG_COLUMNS:
            value = result.get(key)
            cells.append(_pad("-" if value is None else fmt.format(value), width))
        print("  ".join(cells))
    if len(results) > limit:
        print(f"... 他 {len(results) - limit} 件(--limit で表示数を変更)")


# ---------------------------------------------------------------------------
# サブコマンド
# ---------------------------------------------------------------------------
def cmd_parse(args):
    collected = []
    sources = []
    for pattern in args.paths:
        matched = sorted(glob.glob(os.path.expanduser(pattern)))
        sources.extend(matched or [])
        if not matched:
            print(f"[警告] 該当ファイルなし: {pattern}", file=sys.stderr)

    for path in sources:
        with open(path, encoding="utf-8", errors="ignore") as f:
            html_text = f.read()
        try:
            rows = parse_day_html(html_text, store_name=args.store, source_name=path)
        except ValueError as e:
            print(f"[スキップ] {os.path.basename(path)}: {e}", file=sys.stderr)
            continue
        collected.extend(rows)
        print(f"[OK] {rows[0]['date']} {rows[0]['store_name']}: {len(rows)}台 ({os.path.basename(path)})")

    for url in args.url:
        try:
            html_text = fetch_html(url)
            rows = parse_day_html(html_text, store_name=args.store, source_name=url)
        except (RuntimeError, ValueError) as e:
            print(f"[エラー] {url}\n{e}", file=sys.stderr)
            continue
        collected.extend(rows)
        print(f"[OK] {rows[0]['date']} {rows[0]['store_name']}: {len(rows)}台 ({url})")
        time.sleep(args.delay)

    if not collected:
        print("取り込めるデータがありませんでした。", file=sys.stderr)
        return 1

    if args.append:
        collected = load_csv(args.out) + collected
    rows = dedupe(collected)
    write_csv(args.out, rows)

    days = sorted({r["date"] for r in rows})
    print(f"\n{args.out} に {len(rows)}行を書き出しました "
          f"({len(days)}日分: {days[0]} 〜 {days[-1]})")
    return 0


def cmd_daily(args):
    """一覧ページ(と日別データCSV)から、アプリに貼り付ける日別データを作る"""
    rows_by_date = {}
    store = args.store

    if args.list:
        path = os.path.expanduser(args.list)
        with open(path, encoding="utf-8", errors="ignore") as f:
            list_rows, list_store = parse_list_html(f.read(), store_name=args.store)
        if not list_rows:
            print(f"{path} から日別一覧を読み取れませんでした。", file=sys.stderr)
            return 1
        store = store or list_store
        filled = sum(1 for r in list_rows if _is_filled(r["total_diff"]))
        print(f"[一覧ページ] {store}: {len(list_rows)}日分 "
              f"({list_rows[-1]['date']} 〜 {list_rows[0]['date']}) / "
              f"うち総差枚が入っているのは {filled}日")
        for row in list_rows:
            rows_by_date[row["date"]] = row

    if args.csv:
        unit_rows = load_csv(args.csv)
        daily = daily_rows_from_units(unit_rows)
        if not daily:
            print(f"{args.csv} から日別の集計を作れませんでした。", file=sys.stderr)
        else:
            store = store or daily[0]["store_name"]
            print(f"[日別ページ] {len(daily)}日分を計算 "
                  f"({daily[-1]['date']} 〜 {daily[0]['date']})")
            # 日別ページ由来は全項目が埋まっているので、一覧ページの値より優先する
            for row in daily:
                rows_by_date[row["date"]] = row

    if not rows_by_date:
        print("--list か --csv のどちらかを指定してください。", file=sys.stderr)
        return 1

    rows = sorted(rows_by_date.values(), key=lambda r: r["date"], reverse=True)
    if args.days:
        rows = rows[:args.days]

    text = to_paste_text(rows)
    filled = sum(1 for r in rows if _is_filled(r["total_diff"]))
    print(f"\n合計 {len(rows)}日分 / 総差枚まで入っているのは {filled}日分")

    if args.out:
        directory = os.path.dirname(os.path.abspath(args.out))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"{args.out} に書き出しました。")
        print("店舗傾向ページの「日別データの取り込み」欄に、このファイルの中身を貼り付けてください。")
    else:
        print("--- ここから貼り付け ---")
        print(text)
    return 0


def cmd_units(args):
    """日別ページのデータから、アプリに貼り付ける台別データを作る"""
    rows = load_csv(args.csv) if args.csv else []

    if args.html:
        path = os.path.expanduser(args.html)
        with open(path, encoding="utf-8", errors="ignore") as f:
            rows.extend(parse_day_html(f.read(), store_name=args.store, source_name=path))

    if not rows:
        print("--csv か --html のどちらかを指定してください。", file=sys.stderr)
        return 1

    dates = sorted({r["date"] for r in rows}, reverse=True)
    # 台別データは1日ぶんずつ登録する作りなので、日付を1つに決める
    target_date = args.date or dates[0]
    if target_date not in dates:
        print(f"{target_date} のデータがありません。含まれている日付: {', '.join(dates[:10])}",
              file=sys.stderr)
        return 1
    if not args.date and len(dates) > 1:
        print(f"[注意] {len(dates)}日分のデータがあるため、最新の {target_date} を使いました。"
              f"他の日は --date で指定してください。")

    text, report = to_unit_paste_text(rows, date_str=target_date)
    print(f"{report['date']}: {report['units']}台 / {report['machines']}機種")

    if args.out:
        directory = os.path.dirname(os.path.abspath(args.out))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        print(f"{args.out} に書き出しました。")
        print(f"店舗傾向ページの「台別データの取り込み」欄で日付に {report['date']} を指定し、"
              f"このファイルを取り込んでください。")
    else:
        print("--- ここから貼り付け ---")
        print(text)
    return 0


def cmd_agg(args):
    rows = load_csv(args.csv)
    if not rows:
        print(f"{args.csv} が空か存在しません。", file=sys.stderr)
        return 1

    weekday = WEEKDAY_LABELS.index(args.weekday) if args.weekday else None
    selected = filter_rows(
        rows, days=args.days, machine=args.machine, number=args.number,
        weekday=weekday, zorome=args.zorome, since=args.since, until=args.until,
    )
    if not selected:
        print("条件に合うデータがありませんでした。", file=sys.stderr)
        return 1

    days = sorted({r["date"] for r in selected})
    conditions = [f"{len(selected)}行", f"{len(days)}日分({days[0]}〜{days[-1]})"]
    if args.machine:
        conditions.append(f"機種={args.machine}")
    if args.number is not None:
        conditions.append(f"台番号={args.number}")
    if args.weekday:
        conditions.append(f"{args.weekday}曜")
    if args.zorome:
        conditions.append("ゾロ目日のみ")
    print("対象: " + " / ".join(conditions))

    label, results = aggregate(selected, by=args.by)
    results = [r for r in results if r["samples"] >= args.min_samples]
    results.sort(key=lambda r: r["avg_diff"], reverse=True)

    if args.by == "date":
        results.sort(key=lambda r: r["group"])

    print_aggregate(label, results, limit=args.limit)

    if args.out:
        directory = os.path.dirname(os.path.abspath(args.out))
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            writer.writeheader()
            writer.writerows(results)
        print(f"\n集計結果を {args.out} に書き出しました。")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="アナスロの日別ホールデータをCSV化・集計する",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("使い方:")[1] if "使い方:" in __doc__ else "",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_parse = sub.add_parser("parse", help="HTML(またはURL)からCSVを作る")
    p_parse.add_argument("paths", nargs="*", help="保存したHTMLファイル(ワイルドカード可)")
    p_parse.add_argument("--url", action="append", default=[], help="日別ページのURL(複数可)")
    p_parse.add_argument("--store", default="", help="店舗名を明示する(既定はHTMLから判定)")
    p_parse.add_argument("--out", default="data/anaslo.csv", help="出力CSV")
    p_parse.add_argument("--append", action="store_true", help="既存CSVに追記(重複は上書き)")
    p_parse.add_argument("--delay", type=float, default=3.0, help="URL取得の間隔(秒)")
    p_parse.set_defaults(func=cmd_parse)

    p_daily = sub.add_parser("daily", help="アプリに貼り付ける日別データを作る")
    p_daily.add_argument("--list", default="", help="保存した「データ一覧」ページのindex.html")
    p_daily.add_argument("--csv", default="", help="parseで作った台別CSV(あれば総差枚まで埋まる)")
    p_daily.add_argument("--store", default="", help="店舗名を明示する")
    p_daily.add_argument("--days", type=int, default=0, help="新しい方からN日分だけ出す")
    p_daily.add_argument("--out", default="", help="貼り付け用テキストの出力先")
    p_daily.set_defaults(func=cmd_daily)

    p_units = sub.add_parser("units", help="アプリに貼り付ける台別データを作る(1日分)")
    p_units.add_argument("--csv", default="", help="parseで作った台別CSV")
    p_units.add_argument("--html", default="", help="保存した日別ページのHTML(CSVを作らず直接変換する)")
    p_units.add_argument("--date", default="", help="対象日 YYYY-MM-DD(省略時は一番新しい日)")
    p_units.add_argument("--store", default="", help="店舗名を明示する")
    p_units.add_argument("--out", default="", help="貼り付け用テキストの出力先")
    p_units.set_defaults(func=cmd_units)

    p_agg = sub.add_parser("agg", help="CSVを集計する")
    p_agg.add_argument("csv", help="parseで作ったCSV")
    p_agg.add_argument("--by", choices=list(GROUP_KEYS), default="machine", help="集計単位")
    p_agg.add_argument("--days", type=int, default=0, help="直近N日に絞る(0で全期間)")
    p_agg.add_argument("--since", default="", help="開始日 YYYY-MM-DD")
    p_agg.add_argument("--until", default="", help="終了日 YYYY-MM-DD")
    p_agg.add_argument("--machine", default="", help="機種名で絞る(部分一致)")
    p_agg.add_argument("--number", type=int, help="台番号で絞る")
    p_agg.add_argument("--weekday", choices=WEEKDAY_LABELS, help="曜日で絞る")
    p_agg.add_argument("--zorome", action="store_true", help="ゾロ目日・記念日のみ")
    p_agg.add_argument("--min-samples", type=int, default=1, help="この件数未満のグループは除外")
    p_agg.add_argument("--limit", type=int, default=30, help="表示件数")
    p_agg.add_argument("--out", default="", help="集計結果をCSVに書き出す")
    p_agg.set_defaults(func=cmd_agg)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
