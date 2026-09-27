# pachinko_data — パチンコの機種データのJSON

1機種＝1ファイル。ここに置いたJSONが `/pachinko/` の一覧と `/pachinko/<ファイル名>` のページ、
回転率計算の機種選択、`tools/pachinko_build.py` の単体HTMLになる。pushしてデプロイすれば反映される
（画面から登録・編集する手段は持たない。スロットの `machine_data/` と同じ運用）。

## 新台を追加する

1. `_template.json` をコピーして `<id>.json` にする（ファイル名は英小文字・数字・`_`。URLにそのまま使う）
2. 調べた情報を埋める。チャットで頼む場合は「○○を調べてパチンコの機種データに登録して」
3. `python3 tools/pachinko_build.py` で単体HTML（`pachinko_pages/`）も作り直す
4. コミットしてpush

`_` で始まるファイルは機種として扱わない。

## 項目

| キー | 中身 |
|---|---|
| `name` / `maker` | 機種名（必須）・メーカー |
| `spec_type` | ミドル / ライトミドル / 甘デジ / ライト / スマパチ / その他。一覧の絞り込みに使う |
| `hit_prob` / `rush_hit_prob` | 大当り確率・RUSH中確率。**分母だけ**（`1/319.7` → `319.7`） |
| `rush_entry_rate` / `rush_continue_rate` | RUSH突入率・継続率（%の数値） |
| `border_equiv` / `border_28` / `border_33` | 千円あたりのボーダー（等価25玉・28玉・33玉、4円貸し） |
| `yutime_games` / `yutime_spins` / `yutime_note` | 遊タイムの発動回転数・時短回数・メモ |
| `morning_lamp_note` / `technique_note` | 朝一ランプ・リセット判別、止め打ち・ワンツー打法 |
| `effects` | 演出の期待度。`[{"name": "先バレ", "rate": 80, "note": "補足"}]`（`rate` は%、大当り濃厚は100） |
| `note` / `source` / `updated_at` | メモ・出典（URLと確認日）・データの更新日 |

## 書き方のルール

- **推測で数値を入れない。** 分からない項目は `null`（画面では「未登録」）、文章の項目は空文字
- 項目の意味が他の機種とずれるとき（例: RUSH突入率の欄に「バトル突破率」を入れた）は、`note` にそう書く
- 30玉・40玉などのボーダーや確率の内訳は、項目が無いので `note` に書く
