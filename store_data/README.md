# store_data — 店舗情報（基本情報）のJSON

1店舗＝1ファイル。ここに置いたJSONが `/store_info/` の一覧と `/store_info/<id>` のページになる（再起動不要）。
項目の定義の正は `store_info.py` の `FIELDS`。[schema.json](schema.json) はエディタの補完・チェック用。

## 基本情報と営業データの分担

| データ | 置き場所 | 更新のされ方 |
|---|---|---|
| 住所・台数・営業時間・特定日・告知など | このフォルダのJSON | たまに変わる。Webから集めて差分だけ反映 |
| 日別データ（総差枚・平均G数・勝率） | シート `store_daily` | 毎日増える。データ取り込み画面・`tools/anaslo.py` |
| 台別データ（台番・機種・G数・差枚・BB/RB/AT） | シート `store_units` | 同上 |
| 旧イベント日・周年日 | シート `store_events` | 分析・カレンダーが使う |

JSONの `sheet_store_name` がシート側の店舗名と一致していれば、店舗ページに営業データが並ぶ。
設定推測（`common.py`）は今まで通りシートだけを見るので、店舗情報を足しても推測の動きは変わらない。

```
店舗(store_data/<id>.json) ─ sheet_store_name ─┬─ store_daily  日付 → 店全体の結果
                                               └─ store_units  日付 → 台番号 → 機種 → 実績 → 設定推測
```

## 店舗を追加する

```bash
python3 tools/store_collect.py new rakuen_omiya "楽園大宮店" --region 埼玉県
python3 tools/store_collect.py collect rakuen_omiya --url <公式サイトのURL> --reliability 公式          # 差分を確認
python3 tools/store_collect.py collect rakuen_omiya --url <公式サイトのURL> --reliability 公式 --apply  # 保存
```

- 情報源のURLは今のところ人が渡す（検索して公式サイトを見つける部分は自動化していない）
- ほかのページ（P-WORLDなど）も同じ要領で `--reliability ポータル` を付けて足していく
- `requests` で取れないページは、ブラウザで保存したHTMLを `--file` で渡す（`--url` は出典として残す）

## 定期更新

```bash
python3 tools/store_collect.py refresh --apply   # 全店舗の登録済み出典を取り直す
```

cron や n8n から呼ぶ想定。値が変わった項目だけが書き換わり、`history` に変更前・変更後・日時・出典が残る。
Renderの無料プランはディスクが揮発性なので、サーバー上で回しても残らない。ローカルで回してコミットする。

## 書き方のルール

各項目は `{"value": …, "status": …, "source_ids": [...]}` の形。

- **推測で値を入れない。** 分からない項目は `"value": null, "status": "不明"`
- `status` は出典の種類で決まる。人が手で書くときも合わせる
  - `確認済` … 公式サイト・公式SNS（`reliability: "公式"`）で確認できた
  - `未確認` … ポータル・ブログなど公式以外（`reliability: "ポータル"` / `"その他"`）
- 公式で確認済みの値は、公式以外の出典では上書きされない（食い違いとして表示するだけ）
- 収集したページに書かれていなかった項目は「消えた」とはみなさず、前の値を残す
- 収集ツールはAIに根拠の原文（evidence）を返させ、ページ本文に無い値は捨てる

`_` で始まるファイルと `schema.json` は店舗として扱わない。
