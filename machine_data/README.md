# machine_data — 機種情報（解析まとめ）のJSON

1機種＝1ファイル。ここに置いたJSONが `/info/` の一覧と `/info/<id>` のページになる（再起動不要）。
項目の定義は [schema.json](schema.json)。VS Code なら `$schema` の指定で補完とチェックが効く。

## 新台を追加する

1. `_template.json` をコピーして `<id>.json` にする（`id` はファイル名と同じ、英小文字・数字・`_`）
2. 分かった情報を埋める。AIに作らせる場合は資料と一緒にこう頼む:
   > この情報を machine_data/schema.json に沿って machine_data/<id>.json にしてください。
   > 出典に書かれていない数値は推測せず null にし、各項目の source_ids に出典を付けてください。
3. `/info/<id>` を開いて確認。JSONに不備があればページ上部に黄色で出る
4. 設定判別に使うなら、ページの「判別スペックに取り込む」を押す

`_` で始まるファイルと `schema.json` は機種として扱わない。

## 書き方のルール

- **推測で数値を入れない。** 不明は `null`（「未公表」と表示）、調査中は `{"status": "情報確認中"}`
- 確率は分母で持つ（`1/352.0` → `352.0`、`"format": "fraction"`）。％は数値のまま（`"format": "percent"`）
- 設定別の値は `"values": {"2": …, "6": …}`。キーは `settings.list` にある設定だけ
- 出典は `sources` に登録し、各セクションの `source_ids` から参照する（`checked_at` 必須）
- 出典同士で数値が食い違うときは、採用した値と理由を `caution` / `note` に書く

## 判別スペックへの取り込み（`setting_estimation`）

| JSON | 判別DB | 備考 |
|---|---|---|
| `spec.rows` の `key: "payout"` | 機械割 | |
| `probabilities` | 判別要素 | `denom_base: "normal"` で通常時G数を母数にする |
| `ratios` | 選択肢型 | |
| `hints` の `min_setting` / `exact_setting` / `denied_settings` | 確定演出 | 「高設定示唆」のような重み付けだけの示唆は取り込まない |
| `settings.list` | 搭載設定 | 非搭載の設定は判別時に最初から候補外 |
| `observations`（非公式の実戦値） | — | 取り込まない |

同じ機種名が判別DBにあれば上書きする。判別スペック管理で手直しした値も消えるので注意。
