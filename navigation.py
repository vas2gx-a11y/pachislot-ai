"""
画面ナビゲーションの構成定義。

機能が増えるたびにヘッダーの項目を増やすと、PCでは横幅を圧迫し、
スマホでは横スクロールの奥に隠れて気づけなくなる。
そのためヘッダーは「目的」で分けた4カテゴリに固定し、
個々の機能はカテゴリの配下に足していく形にしている。

新しい機能を追加するときは NAV に1行足すだけでよく、
ヘッダー・ドロップダウン・ページ内サブメニュー・スマホのタブすべてに反映される。

各項目:
  endpoint    Flaskのエンドポイント名(url_for に渡すもの)
  label       メニューに出す名前
  icon        絵文字
  description カテゴリのトップページに出す説明
  match       このパスで始まるURLなら、そのカテゴリを選択中として扱う
              (省略時は endpoint から引いたURLで判定する)
"""

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
        ],
    },
    {
        "key": "machines",
        "label": "機種",
        "icon": "🎰",
        "description": "機種のスペックを登録し、期待値の計算に使います。",
        "items": [
            {
                "endpoint": "machines.machines_page",
                "label": "機種スペック・一覧",
                "icon": "🎯",
                "description": "設定別の確率やゲームフローを登録します。設定推測の精度に直結します。",
            },
            {
                "endpoint": "expected_value.index",
                "label": "期待値計算",
                "icon": "🧮",
                "description": "現在のゲーム数から、続行した場合の期待値を計算します。",
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
                "endpoint": "stores.index",
                "label": "店舗の管理",
                "icon": "🏪",
                "description": "店舗の追加・名前の変更・表記ゆれの統合を行います。",
            },
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
                "label": "自分の記録の集計・AI総評",
                "icon": "📊",
                "description": "自分が打った台だけを集計し、AIに傾向を総評してもらいます。",
            },
        ],
    },
]

# カテゴリのトップページのエンドポイント。
# 配下が1つしかないカテゴリはトップを挟まず、その機能へ直接飛ばす。
CATEGORY_INDEX_ENDPOINT = "nav.category"


def visible_items(category):
    """メニューに表示する項目だけを返す"""
    return [item for item in category["items"] if not item.get("hidden")]


def find_category(key):
    for category in NAV:
        if category["key"] == key:
            return category
    return None
