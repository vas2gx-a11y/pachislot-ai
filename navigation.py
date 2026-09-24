"""
画面ナビゲーションの構成定義。

【設計方針】
トップレベルのカテゴリは今後も増える前提で作っている。
そのためナビゲーションは「カテゴリ」と「個別ページ」を明確に分け、
  - PC   : 左サイドバー(カテゴリごとに折りたたみ / サイドバー自体もアイコンのみに縮小可)
  - スマホ: 下部のボトムナビ(よく使うカテゴリ + 「その他」)
というレスポンシブな出し分けにしている。
横に並べるヘッダーメニューは、機能が増えると破綻するため使わない。

【機能を追加するとき】
このファイルの NAV に1行足すだけでよい。
サイドバー・ボトムナビ・カテゴリトップ・現在地のハイライトすべてに反映される。
カテゴリを新設する場合も同様に辞書を1つ足すだけで、
5個目以降はスマホのボトムナビで自動的に「その他」へ回る。

【各項目の意味】
  endpoint    Flaskのエンドポイント名(url_for に渡すもの)
  label       メニューに出す名前
  icon        絵文字
  description カテゴリのトップページに出す説明
  match       このパスで始まるURLならその項目を選択中として扱う(省略時はendpointから解決)
  hidden      メニューには出さないが、現在地の判定には使う(一覧から開く詳細ページなど)
"""

# スマホのボトムナビに直接並べるカテゴリ数。これを超えた分は「その他」にまとめる。
MOBILE_PRIMARY_COUNT = 4

NAV = [
    {
        "key": "data",
        "label": "データ",
        "icon": "📊",
        "description": "自分が打った台の記録を登録して、履歴やグラフで振り返ります。",
        "items": [
            {
                "endpoint": "records.index",
                "label": "データ登録・記録一覧",
                "icon": "📸",
                "description": "スクショや数値から記録を登録し、これまでの記録を一覧で確認します。",
            },
            {
                "endpoint": "records.machine_chart",
                "label": "台別グラフ",
                "icon": "📈",
                "description": "同じ台の記録を並べて、差枚の推移をグラフで見ます。",
                # 記録一覧の各項目から台を指定して開くページなので、メニューには出さない
                "hidden": True,
            },
            # 今後ここに「過去データ」「データ分析」などを追加する
        ],
    },
    {
        "key": "machines",
        "label": "機種",
        "icon": "🎰",
        "description": "機種情報（解析まとめ）を見て、期待値の計算や設定判別に使います。",
        "items": [
            {
                "endpoint": "machine_info.index",
                "label": "機種情報（解析まとめ）",
                "icon": "📖",
                "description": "天井・ゾーン・設定差・設定示唆などの解析情報を出典付きで確認します。設定判別・AIの設定推測もこのデータを使います。",
                # 一覧から各機種のページへ入るので、その配下も選択状態として扱う
                "match": "/info",
            },
            {
                "endpoint": "expected_value.index",
                "label": "期待値計算",
                "icon": "🧮",
                "description": "現在のゲーム数から、続行した場合の期待値を計算します。",
            },
            {
                "endpoint": "judge.index",
                "label": "設定判別",
                "icon": "🎲",
                "description": "小役・終了画面の観測から設定1〜6の確率をベイズ推定し、継続か撤退かを判定します。",
            },
        ],
    },
    {
        "key": "stores",
        "label": "店舗",
        "icon": "🏪",
        "description": "店舗ごとのデータを取り込み、出方のクセを分析します。",
        "items": [
            {
                "endpoint": "store_trends.index",
                "label": "店舗傾向（店の全台）",
                "icon": "🏢",
                "description": "取り込んだホールデータから、曜日・イベント日・機種・台番号ごとの傾向を見ます。",
            },
            {
                "endpoint": "store_info.index",
                "label": "店舗情報",
                "icon": "🏬",
                "description": "住所・台数・営業時間・特定日などを出典付きでまとめ、取り込んだ営業データと並べて見ます。",
                # 一覧から各店舗のページへ入るので、その配下も選択状態として扱う
                "match": "/store_info",
            },
            {
                "endpoint": "store_trends.import_page",
                "label": "データ取り込み",
                "icon": "📥",
                "description": "年間データ・旧イベント日・日別データ・台別データ・CSVをまとめて取り込みます。",
            },
            {
                "endpoint": "calendar.index",
                "label": "イベントカレンダー",
                "icon": "📅",
                "description": "旧イベント日・周年日を月別カレンダーで一覧し、その日の稼働もまとめて確認します。",
            },
            {
                "endpoint": "stores.index",
                "label": "店舗の管理",
                "icon": "🏪",
                "description": "店舗の追加・名前の変更・表記ゆれの統合・いらない店舗の削除を行います。",
            },
            # 今後ここに「イベント分析」などを追加する
        ],
    },
    {
        "key": "analysis",
        "label": "AI分析",
        "icon": "🤖",
        "description": "自分の記録をもとに、狙い目や傾向をまとめます。",
        "items": [
            {
                "endpoint": "store_trends.my_records",
                "label": "自分の記録・AI総評",
                "icon": "📊",
                "description": "自分が打った台だけを集計し、AIに傾向を総評してもらいます。",
            },
            {
                "endpoint": "live_chat.index",
                "label": "実戦チャット",
                "icon": "💬",
                "description": "打ちながら状況を送って、ヤメ時や設定の見込みをAIと相談します。機種情報（解析まとめ）が前提です。",
            },
            # 今後ここに「設定予測」「台選定」「分析履歴」などを追加する
        ],
    },
]


def visible_items(category):
    """メニューに表示する項目だけを返す"""
    return [item for item in category["items"] if not item.get("hidden")]


def find_category(key):
    for category in NAV:
        if category["key"] == key:
            return category
    return None


def mobile_primary():
    """スマホのボトムナビに直接並べるカテゴリ"""
    return NAV[:MOBILE_PRIMARY_COUNT]


def mobile_overflow():
    """ボトムナビに収まらず「その他」へ回すカテゴリ(現状は空)"""
    return NAV[MOBILE_PRIMARY_COUNT:]
