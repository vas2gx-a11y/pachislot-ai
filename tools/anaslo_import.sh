#!/bin/bash
# アナスロの保存ページ(Chromeで「名前を付けて保存」したフォルダ)を、
# このアプリに取り込める形(CSV・台別テキスト・日別テキスト)に自動変換する。
#
# Claudeを介さず、ターミナルから直接実行できるようにしたラッパー。
# 日別ページ("〇〇店 データまとめ")と一覧ページ("〇〇店 データ一覧")の
# どちらを渡しても、中身を見て自動で処理を振り分ける。
#
# 使い方:
#   tools/anaslo_import.sh "~/Downloads/ana-slo.com-2026-08-22-.../"
#   tools/anaslo_import.sh "~/Downloads/ana-slo.com-.../" 楽園大宮店   (店名を明示したい場合)
#
# 出力先: data/anaslo_<店名>.csv (日別ページを取り込むたびに追記・重複は上書き)
#         data/units_<店名>_<日付>.txt   (台別データの取り込み用)
#         data/paste_<店名>.txt          (日別データの取り込み用。CSVと一覧を合成)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ANASLO="$APP_ROOT/tools/anaslo.py"
DATA_DIR="$APP_ROOT/data"
mkdir -p "$DATA_DIR"

if [ $# -lt 1 ]; then
    echo "使い方: $0 <保存したフォルダのパス> [店名]" >&2
    exit 1
fi

SRC_DIR="${1%/}"
STORE_OVERRIDE="${2:-}"
HTML="$SRC_DIR/index.html"

if [ ! -f "$HTML" ]; then
    echo "エラー: $HTML が見つかりません(保存フォルダの中に index.html があるか確認してください)" >&2
    exit 1
fi

TITLE="$(grep -o '<title>[^<]*</title>' "$HTML" | head -1 | sed -E 's/<\/?title>//g')"
echo "対象ページ: ${TITLE:-(タイトル不明)}"

if grep -q '<table id="all_data_table"' "$HTML"; then
    # --- 日別ページ(1日分の全台データ) ---
    STORE="$STORE_OVERRIDE"
    if [ -z "$STORE" ]; then
        STORE="$(echo "$TITLE" | sed -E 's/^[0-9]{4}\/[0-9]{1,2}\/[0-9]{1,2} (.+) データまとめ.*/\1/')"
    fi
    if [ -z "$STORE" ] || [ "$STORE" = "$TITLE" ]; then
        echo "エラー: 店名を判定できませんでした。第2引数で店名を指定してください。" >&2
        exit 1
    fi

    CSV="$DATA_DIR/anaslo_${STORE}.csv"
    echo "→ 日別ページとして処理します(店舗: $STORE)"
    if [ -f "$CSV" ]; then
        python3 "$ANASLO" parse "$HTML" --store "$STORE" --out "$CSV" --append
    else
        python3 "$ANASLO" parse "$HTML" --store "$STORE" --out "$CSV"
    fi

    DATE="$(python3 - "$HTML" <<'PY'
import re, sys
t = open(sys.argv[1], encoding="utf-8", errors="ignore").read(2000)
m = re.search(r"<title>\s*(\d{4})/(\d{1,2})/(\d{1,2})", t)
print(f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}" if m else "")
PY
)"
    UNITS_OUT="$DATA_DIR/units_${STORE}_${DATE}.txt"
    python3 "$ANASLO" units --csv "$CSV" --date "$DATE" --out "$UNITS_OUT"

    # 一覧ページ(店舗トップ)を先に取り込んであれば $DATA_DIR/list_<店名>.html として
    # 置いておくと、そちらの日付とも合成される(無ければCSVだけで日別データを作る)
    DAILY_OUT="$DATA_DIR/paste_${STORE}.txt"
    LIST_HTML="$DATA_DIR/list_${STORE}.html"
    if [ -f "$LIST_HTML" ]; then
        python3 "$ANASLO" daily --list "$LIST_HTML" --csv "$CSV" --out "$DAILY_OUT"
    else
        python3 "$ANASLO" daily --csv "$CSV" --out "$DAILY_OUT"
    fi

    echo
    echo "できたファイル:"
    echo "  台別データ取り込み用: $UNITS_OUT"
    echo "  日別データ取り込み用: $DAILY_OUT"
    echo "  分析用CSV(蓄積分):   $CSV"

elif grep -q 'date-table' "$HTML"; then
    # --- 一覧ページ(店舗トップの日付一覧) ---
    STORE="$STORE_OVERRIDE"
    if [ -z "$STORE" ]; then
        STORE="$(echo "$TITLE" | sed -E 's/^(.+) データ一覧.*/\1/')"
    fi
    echo "→ データ一覧ページとして処理します(店舗: ${STORE:-HTMLから判定})"

    CSV="$DATA_DIR/anaslo_${STORE}.csv"
    DAILY_OUT="$DATA_DIR/paste_${STORE}.txt"
    if [ -f "$CSV" ]; then
        python3 "$ANASLO" daily --list "$HTML" --csv "$CSV" --store "$STORE" --out "$DAILY_OUT"
    else
        python3 "$ANASLO" daily --list "$HTML" --store "$STORE" --out "$DAILY_OUT"
    fi
    # 次に日別ページを取り込んだときに合成できるよう、一覧ページ自体も控えておく
    cp "$HTML" "$DATA_DIR/list_${STORE}.html"

    echo
    echo "できたファイル:"
    echo "  日別データ取り込み用: $DAILY_OUT"

else
    echo "エラー: このページの形式を判定できませんでした" \
         "(all_data_table も date-table も見つかりません)。" >&2
    echo "  保存の仕方が違う可能性があります。ページ全体を「名前を付けて保存」で保存してください。" >&2
    exit 1
fi

echo
echo "あとはアプリの店舗傾向ページで、上記ファイルを取り込んでください。"
