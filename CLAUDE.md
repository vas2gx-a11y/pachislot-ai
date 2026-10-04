# pachislot-ai

パチスロの実戦記録を貯めて、設定推測・店舗の傾向分析をするFlaskアプリ。
データの実体はGoogleスプレッドシート（DBは持たない）。機種と店舗の基本情報だけはリポジトリ内のJSON。Renderにデプロイ。

## 構成

- `app.py` — Flask本体。Blueprint登録、gzip圧縮、ナビの現在地判定のみ。
- `common.py` — **5,000行。ロジックは全部ここ**（下の地図を参照）
- `routes/*.py` — 画面ごとのBlueprint。薄い。`common.py` の関数を呼ぶだけ。
- `navigation.py` — サイドバー/モバイルナビの構成定義。
- `icons.py` — 画面のアイコン（オリジナルのインラインSVG）。テンプレートでは `{{ icon('store') }}`、
  マクロや `navigation.py` にはアイコン名を渡す。絵文字は使わない（OSで絵柄が変わるため）。
  ファビコンは `static/favicon.svg` / `static/apple-touch-icon.png`。
  背景の透かしは `static/demon.webp`（Stable Diffusionで作った墨絵の悪魔。濃さは `base.html` の `body::before` の opacity）。
- `auth.py` — ログインの強制（全ページを before_request で守る）と `admin_required`。
  ユーザーの読み書きは `common.py` の「ユーザー」セクション。
- `templates/` — Jinja2。`base.html` と `_macros.html` が共通土台。
- `static/css/tailwind.css` — Tailwindを事前に作ったCSS（生成物だがコミットする。本番にNodeが無いため）。
  テンプレートで新しいTailwindのクラスを使ったら `tools/tailwind/build.sh` で作り直す（作り直さないとそのクラスは効かない）。
  `static/` のファイルは1年キャッシュさせ、URLに中身の版（`?v=`）を付けている（`app.py`）。参照は必ず `url_for('static', ...)` で書く。
- `static/js/` — 設定判別のブラウザ側コード。他の画面はJSをテンプレートに直書きしているが、
  判別だけは分量と「計算と表示を混ぜない」方針からファイルに切り出している。
- `machine_info.py` / `machine_data/*.json` — 機種情報（解析まとめ）。**機種データの置き場所はここだけ**。
  `/info` の表示、設定判別（`to_client_spec`）、AIの設定推測・Q&A・期待値概算（`to_rule`）、実戦チャットが
  すべてこのJSONから都度組み立てる。画面からの編集手段は持たない。書き方は `machine_data/README.md`。
- `machine_terms.py` — AT・ST・差枚数管理型などタイプの用語説明（スロット・パチンコ共通の辞書）。
  機種ページには、その機種のタイプ・特徴に出てくる用語だけを並べる。新しい用語は辞書に1行足す（機種JSONには書かない）。
- `store_info.py` / `store_data/*.json` — 店舗情報（住所・台数・特定日などの基本情報）。JSONが正で、`/store_info` はそれを描く。
  営業データ（日別・台別）はシートのままで、`sheet_store_name` で紐づけて同じページに並べる。
  店舗の傾向（日別の推移グラフ・勝てる日指数・イベ日の信頼度・曜日と日付の属性・台別・特定日と通常日・並び）もこのページにある（旧 `/store_trends` は店舗情報へリダイレクトするだけ。
  取り込み・自分の記録のページは `routes/store_trends.py` に残っている）。
  日別データは `store_data/daily/<id>.txt`、台別データは `store_data/units/<id>/<日付>.csv`（`tools/anaslo.py parse` の出力）に置いても反映される
  （シートに同じ日があればシート優先。シートに書けない環境から入れるため。台別がある日の日別は全台から計算した値になる）。
  シートに旧イベント日が無い店は、店舗JSONの特定日で通常日と比べる。
  島の構成は店舗JSONのトップレベル `islands`（台番号の範囲。配置表を見て手書き。収集では触らない）に書くと、台別データの島別集計に使われる。
  書き方は `store_data/README.md`。
  「店舗の特徴」の文章（データから見た特徴）は、同じ集計値から `common.build_store_summary` が都度組み立てる（JSONには書かない）。
  店舗の登録はチャットで依頼される運用：Webで調べて根拠つきの収集結果JSON（`data/store_collected/`）を書き、
  `tools/store_collect.py apply` で反映、`tools/store_build.py` で単体HTML（`store_pages/`、生成物）を作る。
- `pachinko_info.py` / `pachinko_data/*.json` — パチンコの機種データ（ボーダー・遊タイム・止め打ち・演出の期待度）。
  スロットの `machine_data/` とは項目が違うので別枠。スロットと同じくJSONが正で、pushすれば `/pachinko` に出る
  （画面からの編集手段は持たない）。書き方は `pachinko_data/README.md`。
  回転率計算（`/pachinko/calc`）は判別と同じくブラウザ側だけで計算し、入力は localStorage に残す。
  `tools/pachinko_build.py` で機種ごとの単体HTML（`pachinko_pages/`、生成物。回転率計算つき）を作る。
- `tools/` — アナスロ(ana-slo.com)からホールデータを取り込むCLI（使い方は `tools/README.md`）と、
  店舗情報をWebから集める `store_collect.py`。
- `data/` — 取り込みの中間ファイル置き場。コミット対象外の作業データ。

## 設定判別（ベイズ推定）

`/judge`。他の機能と違い、データの実体はシートではなく機種情報JSONとブラウザのlocalStorage。

| 置き場所 | 中身 | なぜそこか |
|---|---|---|
| 機種情報JSON（`machine_data/*.json` の `setting_estimation` など） | 機種ごとの判別スペック | 機種データを1か所にまとめるため。入れ子の構造もそのまま書ける |
| localStorage（`static/js/judge.js`） | 店舗条件・判別ログ | その端末で打つ人の持ち物。共有する意味がない |

- `static/js/bayes.js` — 計算だけ。**DOMには触らない**。色やHTMLもここには置かない
  （数値の正しさが結果に直結するため、表示の都合で書き換えられないよう隔離している）。
- `static/js/judge.js` — 判別画面の入出力。`bayes.js` を呼ぶだけ。
- `routes/judge.py` — 判別画面と `/judge/api/machines`。**推定はサーバーでは行わない**
  （入力するたびに事後確率が動くのが使い方なので、1回ごとに往復させると打ちながら使えない）。

- 設定1が無い機種などは `availableSettings`（JSONの `settings.list` と `judge_exclude` から作る）で非搭載設定を持つ。
  判別画面では確定演出と同じ仕組みで非搭載設定を常に候補から外す（`judge.js` の `getConfirmations`）。
  判別エンジンは6要素そろっている前提なので、非搭載設定には搭載中の最低設定の値を入れている（事後確率0なので計算には効かない）。
- JSONに不備がある機種は判別の一覧に出さない。値が揃わず使えなかった項目は `/info/<id>` に出る。

判別スペックは**サーバーが正**。判別ページはレンダリング時にJSONを埋め込んで渡すので、
ブラウザ側にスペックのコピーは持たせない（どちらが正か曖昧になるのを避けるため）。

以前は機種データが machines シート（AIの設定推測用）・SQLite（判別用）・JSON（表示用）の3か所にあり、
同じ機種を3回登録していた。いまはJSONだけ。期待値計算は天井などを自動では使わず、
期待獲得枚数を手入力するか、JSONから作った仕様説明（`to_rule` の `game_flow`）をもとにAIに概算させる。

## ユーザーとデータの分離

ログイン必須。ユーザーは `users` シート。管理者は1人で `tools/create_admin.py` で作り（user_id は `owner` 固定）、
メンバーは管理者が `/members` から追加する（仮パスワードを伝え、本人が `/account` で変える）。

| データ | 誰のものか |
|---|---|
| records / chat_logs / unit_notes | 登録した本人だけ。各行の `user_id` 列で分ける（空の行は複数人対応前のもので、管理者の持ち物） |
| 店舗のお気に入り | 本人だけ。`users` シートの `favorite_stores` 列（店舗IDのカンマ区切り）。端末をまたいで同じにするためシートに置く |
| ホールデータ（日別・台別・年間・旧イベント日）、機種・店舗JSON（パチンコ含む） | 全員で共有。取り込み・店舗管理は管理者だけ |

- 分離は `load_records()` などの読み込み関数の中でやっている（`_own_rows`）。呼び出し側で絞らなくていい。
  **全員分の行が欲しい処理（店舗名の変更など）はシートを直接読む**。
- `_cached_by_data_version` のキーにはユーザーが入っている（自分の記録の集計が他人に混ざらないように）。
- 新しいページは既定でログイン必須。管理者だけにするなら `@auth.admin_required`、
  メニュー項目は `navigation.py` で `"admin_only": True`。

## common.py の地図

**全体を読まないこと。**該当セクションだけ `sed -n 'A,Bp' common.py` で開く。

| 行 | 内容 |
|---|---|
| 25-259 | インメモリキャッシュ、集計キャッシュ、各種設定定数 |
| 260-828 | スプレッドシート接続、各シートのload/save |
| 829-906 | 画像解析（Gemini） |
| 907-1030 | URLから本文テキストを取る（`tools/store_collect.py` 用） |
| 1031-1487 | 設定推測ロジック（機種データは `find_machine_rule` → `machine_info.to_rule`） |
| 1488-1617 | 分析結果へのQ&A（セッション単位のチャット） |
| 1618-1713 | 期待値計算（天井/ゾーン狙い） |
| 1714-2453 | 店舗の傾向分析（自分の実戦記録から集計） |
| 2454-2666 | 店舗の年間データ（外部集計の登録・読み込み） |
| 2667-3034 | ホールデータの貼り付け取り込み、日別データの読み込み（シート＋store_data/daily・units） |
| 3035-3575 | 旧イベント日・周年日の解釈（日付・のつく日・ゾロ目・曜日のルール）、日別データの集計とイベ日の信頼度 |
| 3576-3852 | 日付の属性（祝日・休前日・月末・年末年始など。祝日は自前で計算）と勝てる日指数（`build_store_day_forecast`） |
| 3853-4307 | 台別データ（機種・台番号ごと）の貼り付け取り込み、読み込み（シート＋store_data/units） |
| 4308-5056 | 取り込みログ（取り込みの取り消し）、台別データの集計（末尾・端台・機種・島・特定日と通常日・並び） |
| 5057-5264 | 店舗の特徴の文章（日別・台別の集計から組み立てる。AIは使わない） |
| 5265-5738 | イベントカレンダー（月別。イベント日には店舗情報ページと同じ信頼度を付ける） |
| 5739-6169 | 実戦チャット（打ちながら相談、店舗情報・ホールデータ（日別・台別の集計・勝てる日指数）・損益分岐の参照、終了時の記録・振り返り・台メモ保存） |
| 6170-6424 | ユーザー（ログイン・メンバー管理、ユーザーごとのデータ分離、店舗のお気に入り） |
| 6425-6587 | AIの使用量（Gemini呼び出しは `_post_gemini` を通す。アカウント別の使用料の概算） |

## 決まりごと

- コメントは日本語。**なぜそうしたか**を書く（何をしているかはコードを読めば分かる）。
  既存のコメントの密度と口調に合わせる。
- セクション区切りは `# ---...---` + 見出し + 意図の説明、の形を踏襲する。
  `common.py` にセクションを増やしたら、上の地図も直す。
- シートを読む処理は必ずキャッシュ層（`_cache_get` / `_cached_by_data_version`）を通す。
  書き込んだら対応するキャッシュを `_cache_invalidate` する。
- 保存系の関数は「同じキーなら上書き」で書く（取り込みのやり直しが効くように）。

## 環境変数

`SPREADSHEET_ID` / `GOOGLE_SERVICE_ACCOUNT_JSON` / `GEMINI_API_KEY` / `FLASK_SECRET_KEY` が必須
（`FLASK_SECRET_KEY` が無いと起動はするが、再起動のたびに全員ログアウトされる）。
AI使用料の概算の単価は `GEMINI_PRICE_INPUT_PER_M` / `GEMINI_PRICE_OUTPUT_PER_M`（USD/100万トークン）、
円換算は `USD_JPY_RATE`（既定150）で上書きできる。
シート名は `SHEET_NAME`、`STORE_DAILY_SHEET_NAME` など個別に上書き可（既定値は `common.py` 冒頭）。

## 注意

- ana-slo.com はCloudflareでbotを弾くので、`requests` / `curl` では取れない。
  取得はブラウザ側、解析はPython側という分担になっている（`tools/README.md`）。
- `?refresh=1` を付けるとキャッシュを捨ててシートから読み直す。
- Renderの無料プランはディスクが揮発性なので、サーバー上でファイルを書き換えても再デプロイで消える。
  機種・店舗のJSONを画面から編集できるようにしないのはこのため（JSONを直してデプロイする）。
- スプレッドシートに残っている `machines` シートはもう読んでいない（中身は参考として残してある）。
