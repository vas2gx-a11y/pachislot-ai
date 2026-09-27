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

## Xの狙い目・やめどき（`sns_tips`）

X(旧Twitter)の投稿から拾った立ち回り情報。解析サイトの値（`quit_timing` / `zones` など）とは分けて持ち、
ページでは「非公式」として別セクションに出す（個人の実戦値・考察が解析値と混ざって読まれないように）。

- `kind` は `aim`（狙い目）/ `quit`（やめどき）/ `note`（その他）
- `text` は投稿の要約。転載はしない
- 投稿1件を `sources` に1件登録する（`name` は `X @アカウント名`、`url` は投稿のURL）
- AIの設定推測・期待値概算（`to_rule`）には渡さない。実戦チャットにはJSONごと渡るので参照される

## ゲームフロー図（`game_flow`）

解析サイトの「ゲームフロー」画像と同じ、通常時 → CZ・ボーナス → AT → 特化ゾーン の図を `/info/<id>` に出す。
箱（`nodes`）と矢印（`edges`）だけを書けば、段の並べ方・矢印の引き回しはページ側で自動で決まる（座標は書かない）。

```json
"game_flow": {
  "nodes": [
    { "id": "normal", "label": "通常時", "kind": "normal", "note": "レア小役・規定G数で抽選" },
    { "id": "cz", "label": "レミニセンス", "kind": "cz", "note": "8G・成功期待度 約50%" },
    { "id": "at", "label": "東京喰種咬", "kind": "at", "note": "初期 差枚 約150枚" }
  ],
  "edges": [
    { "from": "normal", "to": "cz" },
    { "from": "cz", "to": "at", "label": "成功" },
    { "from": "cz", "to": "normal", "label": "失敗" }
  ],
  "source_ids": ["pworld"]
}
```

- 先頭の箱（ふつうは通常時）が一番上になる。`kind` は箱の色（`normal` / `cz` / `bonus` / `at` / `special`=特化ゾーン / `other`=前兆・バトルなど）、
  帯の文言を変えたいときは `tag`（「上位CZ」「ST」など）
- 前の状態に戻る矢印（CZ失敗→通常時、特化ゾーン→AT など）は線を引かず、元の箱の下に「↩ 失敗 → 通常時」と書かれる
- `note` と矢印の `label` は短く。細かい条件は `features` に書く
- 出典に無い遷移は矢印にしない。突入契機が分からない箱は `row`（この段より上に置かない）で位置だけ合わせ、`notes` に「情報確認中」と書く
- AIの設定推測・Q&A・期待値概算（`to_rule` の `game_flow`）にもそのまま渡る

## このJSONを使う機能

機種データはこのJSONだけが正。画面から編集する手段はなく、JSONを直してデプロイする。
置いた時点で次の機能すべてに反映される（取り込み操作は不要）。

| 機能 | 使うところ | 変換 |
|---|---|---|
| 機種情報ページ `/info` | 全体 | そのまま表示（`game_flow` は `machine_info.flow_layout` で図にする） |
| 設定判別 `/judge` | `setting_estimation`・`settings.list`・`spec` の機械割 | `machine_info.to_client_spec` |
| 記録登録時のAI設定推測・Q&A・期待値のAI概算 | `spec`・`setting_estimation`・`game_flow`・`features` など | `machine_info.to_rule` |
| 実戦チャット `/chat` | 全体 | JSONをそのままAIに渡す |

機種名は `name` と `aliases` の両方で探す（記録登録の手入力に表記ゆれがあるため）。
よく使う略称は `aliases` に入れておくと、設定推測やチェックリストが効く。

### 設定判別に使われる項目（`setting_estimation`）

| JSON | 判別での扱い | 備考 |
|---|---|---|
| `spec.rows` の `key: "payout"` | 機械割 | |
| `probabilities` | 判別要素 | `denom_base: "normal"` で通常時G数を母数にする |
| `ratios` | 選択肢型 | |
| `hints` の `min_setting` / `exact_setting` / `denied_settings` | 確定演出 | 「高設定示唆」のような重み付けだけの示唆は使わない |
| `settings.list` | 搭載設定 | 非搭載の設定は判別時に最初から候補外 |
| `observations`（非公式の実戦値） | — | 使わない |

値が揃わず使えなかった項目は、`/info/<id>` の「設定判別に使っていない項目」に出る。
JSONに不備（出典IDの欠け・非搭載設定の値など）がある機種は、判別の一覧に出さない。

### 記録登録の示唆項目チェックリスト

`hints` のうち確定・否定系（`min_setting` / `exact_setting` / `denied_settings`）が「あり/なし」、
`probabilities` が「回数」の入力欄になる。
