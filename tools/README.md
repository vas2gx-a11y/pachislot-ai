# アナスロ データ取り込みツール

アナスロ(ana-slo.com)の日別ホールデータを取得してCSV化し、機種別・台番別・曜日別などに集計する。

日別ページ(例 `https://ana-slo.com/2026-08-22-楽園大宮店-data/`)のHTMLには
`<table id="all_data_table">` という全台データの表がそのまま入っている。

| 機種名 | 台番号 | G数 | 差枚 | BB | RB | ART | 合成確率 | BB確率 | RB確率 | ART確率 |
|---|---|---|---|---|---|---|---|---|---|---|
| ネオアイムジャグラーEX | 2084 | 7,482 | -1,571 | 20 | 29 | 0 | 1/152.7 | 1/374.1 | 1/258.0 | 1/0.0 |

画面上はボタンを押すまで隠れているが、HTMLには最初から書き出されているので、
画像解析ではなくHTMLから直接読める。楽園大宮店の場合、1日あたり約1,100台分。

---

## 前提: Pythonから直接は取得できない

ana-slo.com はCloudflareのbot対策が有効で、`requests` や `curl` からのアクセスは
**403 (Just a moment...)** で弾かれる。そのため取得はブラウザ側で行い、解析・集計をPython側で行う。

```
Chromeで開いているページ          →  anaslo_fetch.js  →  CSV
                                                          ↓
                                                      anaslo.py agg  →  機種別/台番別/曜日別の集計
```

---

## 手順1: ブラウザでCSVを作る

1. Chromeで https://ana-slo.com/ の任意のページを開く
2. `F12` → **Console** タブ
3. [`anaslo_fetch.js`](anaslo_fetch.js) の中身を全部コピーし、先頭の `CONFIG` を書き換えて貼り付け、Enter

```js
const CONFIG = {
  store: '楽園大宮店',   // 店舗名(アナスロのページ表記どおりに)
  days: 30,              // 何日分さかのぼるか
  endDate: '',           // 起点日 'YYYY-MM-DD'(空なら昨日から)
  delayMs: 3000,         // 1日分ごとの待ち時間(ミリ秒)
};
```

4. 進捗がコンソールに出て、終わるとCSVが自動でダウンロードされる

```
[1/30] 2026-07-24: 1104台 / 総差枚 -35,721
[2/30] 2026-07-25: 1102台 / 総差枚 +12,043
...
完了: 33,061行 / 30日分
```

休業日など存在しない日は自動でスキップされる。
1日分ずつ間隔を空けて順番に取得するので、30日分でおよそ2〜3分かかる。
`delayMs` は短くしすぎないこと(相手のサーバーに負荷をかけない)。

> 別のやり方として、日別ページをブラウザで「名前を付けて保存」してから
> `anaslo.py parse` にかけてもよい(下記)。

## 手順2: 集計する

ダウンロードしたCSVをそのまま渡す。

```bash
python3 tools/anaslo.py agg ~/Downloads/anaslo_楽園大宮店_2026-07-24_2026-08-22.csv --by machine
```

```
対象: 33061行 / 30日分(2026-07-24〜2026-08-22)

[機種別]  ※平均差枚の高い順
対象                          件数    総差枚       平均差枚   平均G数    勝率     最高       最低       合成
------------------------------------------------------------------------------------------------------------
マギアレコード 魔法少女まど…  840     +48,297      +1,725     8,161      50.0%    +9,524     -5,120     1/117.5
...
```

### 集計オプション

| オプション | 意味 |
|---|---|
| `--by machine` | 機種別(既定) |
| `--by number` | 台番別 |
| `--by machine-number` | 機種+台番別 |
| `--by date` | 日付別 |
| `--by weekday` | 曜日別 |
| `--by store` | 店舗全体 |
| `--days 30` | 直近30日に絞る |
| `--since 2026-07-01 --until 2026-07-31` | 期間指定 |
| `--machine ジャグラー` | 機種名の部分一致で絞る |
| `--number 2419` | 台番号で絞る |
| `--weekday 土` | 曜日で絞る |
| `--zorome` | ゾロ目日(11日・22日)と月日ゾロ目(8/8など)のみ |
| `--min-samples 5` | サンプル数が少ないグループを除外 |
| `--limit 50` | 表示件数 |
| `--out path.csv` | 集計結果もCSVに書き出す |

よく使う組み合わせ:

```bash
# 直近90日、ジャグラーのシマを台番別に(5日以上のデータがある台だけ)
python3 tools/anaslo.py agg data/anaslo.csv --by number --machine ジャグラー --days 90 --min-samples 5

# ゾロ目日に強い機種を探す
python3 tools/anaslo.py agg data/anaslo.csv --by machine --zorome --min-samples 10

# 曜日ごとの出方の差
python3 tools/anaslo.py agg data/anaslo.csv --by weekday --days 90
```

## 手順3(任意): 保存したHTMLから作る場合

ブラウザで日別ページを保存した場合は `parse` を使う。ファイル名か本文から日付・店舗名を判定する。

```bash
python3 tools/anaslo.py parse ~/Downloads/*.html --out data/anaslo_楽園大宮店.csv
python3 tools/anaslo.py parse ~/Downloads/2026-08-23*.html --out data/anaslo_楽園大宮店.csv --append
```

`--append` は既存CSVに追記する。同じ 日付×店舗×台番号 の行は新しい方で上書きされるので、
同じ日を2回取り込んでも重複しない。

---

## CSVの形式

`anaslo_fetch.js` と `anaslo.py parse` は同じ列を出す。

| 列 | 内容 |
|---|---|
| `date` | 対象日 `YYYY-MM-DD` |
| `store_name` | 店舗名 |
| `machine_number` | 台番号 |
| `machine_name` | 機種名 |
| `games` | G数 |
| `diff` | 差枚 |
| `bb` / `rb` / `art` | BB・RB・ART回数 |
| `total_rate` | 合成確率の分母(`1/152.7` → `152.7`) |
| `bb_rate` / `rb_rate` / `art_rate` | 各確率の分母 |

確率の分母は、当たり0回で `1/0.0` と表示される欄を**空**にしている。
これは「確率0」ではなく算出不能な値で、そのまま0として扱うと平均が壊れるため。

集計時の合成確率は「平均の平均」ではなく **総G数 ÷ 総当たり回数**で出している。

---

## 設定予測アプリとの連携

`--by store` の出力は、アプリの店舗傾向ページ(`/store_trends`)にある
**総差枚・平均差枚・平均G数・勝率**の入力欄にそのまま入る。
これまで画像から読み取っていた値を、実データから直接出せる。

```bash
python3 tools/anaslo.py agg data/anaslo_楽園大宮店.csv --by store --days 90
```

Pythonから使う場合は関数を直接呼べる(標準ライブラリのみで動作、追加依存なし)。

```python
import sys
sys.path.append("tools")
import anaslo

rows = anaslo.load_csv("data/anaslo_楽園大宮店.csv")
recent = anaslo.filter_rows(rows, days=30, machine="ジャグラー")
label, results = anaslo.aggregate(recent, by="number")
```

---

## 注意

- 取得したデータは個人の分析用に使うこと。再配布や転載は避ける。
- 取得間隔(`delayMs`)を詰めない。まとめて取るのは最初の1回だけにして、
  以降は前日分を1日ずつ足していくのが相手にも自分にも楽。
- ページの構造が変わると解析に失敗する。その場合は日別ページのHTMLで
  `all_data_table` を検索して、表の列構成を確認する。
