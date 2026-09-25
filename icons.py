"""
画面で使うアイコン（オリジナルのSVG）。

以前は絵文字をそのまま使っていたが、OS・ブラウザで絵柄が変わり（Windowsだと別物になる）、
色もページの配色と合わないので、線の太さと角の丸みを揃えた自前のアイコンに置き換えた。

- 24x24 の線画。色は currentColor なので、置いた場所の文字色に従う。
- 塗りの薄い面（DUO）を1枚だけ重ねて、線画だけの味気なさを抑えている。
- 画像ファイルにせずインラインSVGにしているのは、文字色に追従させるため
  （<img> で読むと currentColor が効かない）と、アイコンごとのリクエストを増やさないため。

テンプレートでは `{{ icon('store') }}` と書く。マクロ（card など）にはアイコン名を文字列で渡す。
"""

from markupsafe import Markup, escape

# 薄い塗り面。線の下に敷く
_DUO = 'fill="currentColor" fill-opacity=".16" stroke="none"'

ICONS = {
    # --- ブランド・機種 ---------------------------------------------------
    # ロゴ: 3つのリール窓の真ん中に「7」
    "logo": (
        f'<rect x="2.5" y="4.5" width="19" height="15" rx="3.5" {_DUO}/>'
        '<rect x="2.5" y="4.5" width="19" height="15" rx="3.5"/>'
        '<path d="M8.5 4.5v15M15.5 4.5v15"/>'
        '<path d="M10.6 9.2h2.8l-2 5.6"/>'
        '<path d="M4.8 12h1.6M17.6 12h1.6"/>'
    ),
    # スロット筐体: リール3つ＋レバー
    "slot": (
        f'<rect x="3" y="7" width="14" height="9" rx="1.5" {_DUO}/>'
        '<rect x="3" y="3.5" width="14" height="17" rx="2.5"/>'
        '<path d="M3 7h14M3 16h14M7.7 7v9M12.3 7v9"/>'
        '<path d="M17 12h2.5V6.5"/><circle cx="19.5" cy="5" r="1.5"/>'
    ),
    # G数（回転数）: 回るリール
    "counter": (
        f'<circle cx="12" cy="12" r="5" {_DUO}/>'
        '<path d="M20 12a8 8 0 1 1-2.3-5.6"/><path d="M20 4v3.5h-3.5"/>'
        '<path d="M10.5 10h3l-2 4.5"/>'
    ),
    # メダル（差枚）
    "coin": (
        f'<ellipse cx="12" cy="8" rx="7" ry="3" {_DUO}/>'
        '<ellipse cx="12" cy="8" rx="7" ry="3"/>'
        '<path d="M5 8v4c0 1.7 3.1 3 7 3s7-1.3 7-3V8"/>'
        '<path d="M5 12v4c0 1.7 3.1 3 7 3s7-1.3 7-3v-4"/>'
    ),
    "dice": (
        f'<rect x="4" y="4" width="16" height="16" rx="3.5" {_DUO}/>'
        '<rect x="4" y="4" width="16" height="16" rx="3.5"/>'
        '<circle cx="8.6" cy="8.6" r=".9" fill="currentColor"/>'
        '<circle cx="15.4" cy="8.6" r=".9" fill="currentColor"/>'
        '<circle cx="12" cy="12" r=".9" fill="currentColor"/>'
        '<circle cx="8.6" cy="15.4" r=".9" fill="currentColor"/>'
        '<circle cx="15.4" cy="15.4" r=".9" fill="currentColor"/>'
    ),
    "calculator": (
        f'<rect x="7.5" y="5.5" width="9" height="4" rx="1" {_DUO}/>'
        '<rect x="5" y="3" width="14" height="18" rx="2.5"/>'
        '<rect x="7.5" y="5.5" width="9" height="4" rx="1"/>'
        '<path d="M8.5 13h.01M12 13h.01M15.5 13h.01M8.5 17h.01M12 17h.01M15.5 17h.01" stroke-width="2.4"/>'
    ),
    "book": (
        f'<path d="M12 6.5C10 5 7 4.5 3.5 5v13c3.5-.5 6.5 0 8.5 1.5" {_DUO}/>'
        '<path d="M12 6.5C10 5 7 4.5 3.5 5v13c3.5-.5 6.5 0 8.5 1.5 2-1.5 5-2 8.5-1.5V5c-3.5-.5-6.5 0-8.5 1.5z"/>'
        '<path d="M12 6.5v13"/>'
    ),

    # --- データ・グラフ -----------------------------------------------------
    "chart": (
        f'<rect x="6" y="11" width="3" height="7" rx=".8" {_DUO}/>'
        f'<rect x="15" y="8" width="3" height="10" rx=".8" {_DUO}/>'
        '<path d="M3.5 20.5h17"/>'
        '<rect x="6" y="11" width="3" height="7" rx=".8"/>'
        '<rect x="10.5" y="5" width="3" height="13" rx=".8"/>'
        '<rect x="15" y="8" width="3" height="10" rx=".8"/>'
    ),
    "trend_up": (
        f'<path d="M3.5 17l5.5-5.5 4 4 7-7.5V20H3.5z" {_DUO}/>'
        '<path d="M3.5 17l5.5-5.5 4 4 7-7.5"/><path d="M15 8h5v5"/>'
    ),
    "trend_down": (
        f'<path d="M3.5 7l5.5 5.5 4-4 7 7.5V20H3.5z" {_DUO}/>'
        '<path d="M3.5 7l5.5 5.5 4-4 7 7.5"/><path d="M15 16h5v-5"/>'
    ),
    "calendar": (
        f'<path d="M3.5 9.5h17V7a2 2 0 0 0-2-2h-13a2 2 0 0 0-2 2z" {_DUO}/>'
        '<rect x="3.5" y="5" width="17" height="15.5" rx="2"/>'
        '<path d="M3.5 9.5h17M8 3v4M16 3v4"/>'
        '<path d="M7.5 13.5h.01M12 13.5h.01M16.5 13.5h.01M7.5 17h.01M12 17h.01" stroke-width="2.4"/>'
    ),
    "hash": (
        f'<rect x="3.5" y="3.5" width="17" height="17" rx="3.5" {_DUO}/>'
        '<rect x="3.5" y="3.5" width="17" height="17" rx="3.5"/>'
        '<path d="M10 7.5l-1.2 9M15.2 7.5l-1.2 9M7.5 10.2h9.5M7 13.8h9.5"/>'
    ),
    # 末尾の数字: 数字の一番右の桁を強調
    "digit": (
        f'<rect x="13" y="6" width="7.5" height="12" rx="2" {_DUO}/>'
        '<rect x="13" y="6" width="7.5" height="12" rx="2"/>'
        '<path d="M16.8 9.5v5M15.5 10.3l1.3-.8"/>'
        '<path d="M4 9.5v5M2.9 10.3L4 9.5M7 10.2c.3-.5.8-.7 1.3-.7.8 0 1.4.5 1.4 1.3 0 1.3-2.7 2.4-2.7 3.7h2.8"/>'
    ),
    "medal": (
        '<path d="M8 3l2.5 6M16 3l-2.5 6"/>'
        f'<circle cx="12" cy="15" r="5.5" {_DUO}/>'
        '<circle cx="12" cy="15" r="5.5"/>'
        '<path d="M12 12.3l.9 1.8 2 .3-1.4 1.4.3 2-1.8-1-1.8 1 .3-2-1.4-1.4 2-.3z" stroke-width="1.2"/>'
    ),
    "pin": (
        f'<path d="M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 0 1 13 0c0 5.4-6.5 11-6.5 11z" {_DUO}/>'
        '<path d="M12 21s-6.5-5.6-6.5-11a6.5 6.5 0 0 1 13 0c0 5.4-6.5 11-6.5 11z"/>'
        '<circle cx="12" cy="10" r="2.3"/>'
    ),
    "target": (
        f'<circle cx="12" cy="12" r="8.5" {_DUO}/>'
        '<circle cx="12" cy="12" r="8.5"/><circle cx="12" cy="12" r="4.5"/>'
        '<circle cx="12" cy="12" r="1" fill="currentColor"/>'
    ),

    # --- 店舗 ---------------------------------------------------------------
    # 店舗（管理）: 日よけのある店構え
    "store": (
        f'<path d="M3.5 9.5l1.3-5h14.4l1.3 5" {_DUO}/>'
        '<path d="M3.5 9.5l1.3-5h14.4l1.3 5"/>'
        '<path d="M3.5 9.5a2.1 2.1 0 0 0 4.2 0 2.1 2.1 0 0 0 4.3 0 2.1 2.1 0 0 0 4.3 0 2.1 2.1 0 0 0 4.2 0"/>'
        '<path d="M5 11.5v9h14v-9"/><path d="M10 20.5v-5h4v5"/>'
    ),
    # 店舗情報: 店構え＋案内板
    "store_info": (
        f'<rect x="4" y="3.5" width="16" height="6" rx="1.5" {_DUO}/>'
        '<rect x="4" y="3.5" width="16" height="6" rx="1.5"/>'
        '<path d="M8 6.5h8"/>'
        '<path d="M5.5 9.5v11h13v-11"/>'
        '<rect x="8" y="12.5" width="3.5" height="3" rx=".5"/>'
        '<path d="M14 20.5v-8h2.5v8"/>'
    ),
    # 店舗傾向（ホール全体）: 大きな建物
    "building": (
        f'<rect x="4" y="3.5" width="10" height="17" rx="1" {_DUO}/>'
        '<rect x="4" y="3.5" width="10" height="17" rx="1"/>'
        '<path d="M14 9.5h5a1 1 0 0 1 1 1v10H14"/>'
        '<path d="M7 7h1M10 7h1M7 10.5h1M10 10.5h1M7 14h1M10 14h1M16.5 13h1M16.5 16.5h1" stroke-width="2"/>'
        '<path d="M2.5 20.5h19"/>'
    ),
    "sunrise": (
        f'<path d="M5.5 17a6.5 6.5 0 0 1 13 0z" {_DUO}/>'
        '<path d="M5.5 17a6.5 6.5 0 0 1 13 0M2.5 17h19M5.5 20.5h13"/>'
        '<path d="M12 3.5v3M4.6 8.6l2 2M19.4 8.6l-2 2"/>'
    ),
    "cake": (
        f'<rect x="4" y="12" width="16" height="8.5" rx="1.5" {_DUO}/>'
        '<rect x="4" y="12" width="16" height="8.5" rx="1.5"/>'
        '<path d="M4 15.5c1.3 1 2.7 1 4 0s2.7-1 4 0 2.7 1 4 0 2.7-1 4 0"/>'
        '<path d="M12 12V8.5"/><path d="M12 3.5c1 1 1.4 1.8 1.4 2.5a1.4 1.4 0 0 1-2.8 0c0-.7.4-1.5 1.4-2.5z"/>'
    ),
    # イベント日: クラッカー
    "party": (
        f'<path d="M4 20l4.5-12 7.5 7.5z" {_DUO}/>'
        '<path d="M4 20l4.5-12 7.5 7.5z"/>'
        '<path d="M13 3.5c.3 1.5 0 2.5-1 3.5M20.5 11c-1.5-.3-2.5 0-3.5 1M15.5 8.5l3-3"/>'
        '<path d="M18.5 3h.01M21 7.5h.01M16 3.5h.01" stroke-width="2.4"/>'
    ),

    # --- AI・会話 -----------------------------------------------------------
    # AI: きらめき
    "ai": (
        f'<path d="M10 3.5l1.7 4.8 4.8 1.7-4.8 1.7L10 16.5l-1.7-4.8L3.5 10l4.8-1.7z" {_DUO}/>'
        '<path d="M10 3.5l1.7 4.8 4.8 1.7-4.8 1.7L10 16.5l-1.7-4.8L3.5 10l4.8-1.7z"/>'
        '<path d="M18 14.5l.8 2.2 2.2.8-2.2.8-.8 2.2-.8-2.2-2.2-.8 2.2-.8z"/>'
    ),
    "chat": (
        f'<path d="M4 6a2.5 2.5 0 0 1 2.5-2.5h11A2.5 2.5 0 0 1 20 6v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 4v-4A2.5 2.5 0 0 1 4 14z" {_DUO}/>'
        '<path d="M4 6a2.5 2.5 0 0 1 2.5-2.5h11A2.5 2.5 0 0 1 20 6v8a2.5 2.5 0 0 1-2.5 2.5H10l-4.5 4v-4A2.5 2.5 0 0 1 4 14z"/>'
        '<path d="M8.5 10h.01M12 10h.01M15.5 10h.01" stroke-width="2.4"/>'
    ),
    "bulb": (
        f'<path d="M9 16.5c0-2-3-3.5-3-7a6 6 0 0 1 12 0c0 3.5-3 5-3 7z" {_DUO}/>'
        '<path d="M9 16.5c0-2-3-3.5-3-7a6 6 0 0 1 12 0c0 3.5-3 5-3 7z"/>'
        '<path d="M9.5 19.5h5M10.5 22h3"/>'
    ),
    "camera": (
        f'<circle cx="12" cy="13" r="3.8" {_DUO}/>'
        '<path d="M3.5 9a2 2 0 0 1 2-2h2l1.5-2.5h6L16.5 7h2a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2h-13a2 2 0 0 1-2-2z"/>'
        '<circle cx="12" cy="13" r="3.8"/>'
    ),
    "image": (
        f'<path d="M3.5 17l5-5 4 4 2.5-2.5 5.5 5.5" {_DUO}/>'
        '<rect x="3.5" y="4.5" width="17" height="15" rx="2"/>'
        '<path d="M3.5 16.5l5-5 4 4 2.5-2.5 5.5 5.5"/><circle cx="15.5" cy="9" r="1.5"/>'
    ),

    # --- 記録・書類 ---------------------------------------------------------
    "memo": (
        f'<path d="M5 3.5h10l4 4v13H5z" {_DUO}/>'
        '<path d="M15 3.5H6a1 1 0 0 0-1 1v15a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-12z"/>'
        '<path d="M15 3.5v4h4M8.5 12h7M8.5 15.5h5"/>'
    ),
    "file": (
        f'<path d="M6 3.5h8.5l4 4V20a.5.5 0 0 1-.5.5H6.5A.5.5 0 0 1 6 20z" {_DUO}/>'
        '<path d="M14.5 3.5H7a1 1 0 0 0-1 1v15a1 1 0 0 0 1 1h10.5a1 1 0 0 0 1-1v-12z"/>'
        '<path d="M14.5 3.5v4h4"/><path d="M9 13h6M9 16.5h6M9 9.5h2"/>'
    ),
    "clipboard": (
        f'<rect x="5" y="5" width="14" height="16" rx="2" {_DUO}/>'
        '<rect x="5" y="5" width="14" height="16" rx="2"/>'
        '<rect x="8.5" y="3" width="7" height="4" rx="1"/>'
        '<path d="M8.5 11.5h7M8.5 15h7"/>'
    ),
    "receipt": (
        f'<path d="M5.5 3.5h13v17l-2.2-1.5-2.1 1.5-2.2-1.5-2.2 1.5-2.1-1.5-2.2 1.5z" {_DUO}/>'
        '<path d="M5.5 3.5h13v17l-2.2-1.5-2.1 1.5-2.2-1.5-2.2 1.5-2.1-1.5-2.2 1.5z"/>'
        '<path d="M9 8h6M9 11.5h6M9 15h3.5"/>'
    ),
    "pencil": (
        f'<path d="M4 20l1-4.5L15.5 5l3.5 3.5L8.5 19z" {_DUO}/>'
        '<path d="M4 20l1-4.5L15.5 5a1.4 1.4 0 0 1 2 0l1.5 1.5a1.4 1.4 0 0 1 0 2L8.5 19z"/>'
        '<path d="M13.5 7l3.5 3.5"/>'
    ),
    "import": (
        f'<path d="M3.5 14.5h4.5l1.5 2.5h5l1.5-2.5h4.5V19a1.5 1.5 0 0 1-1.5 1.5h-14A1.5 1.5 0 0 1 3.5 19z" {_DUO}/>'
        '<path d="M3.5 14.5h4.5l1.5 2.5h5l1.5-2.5h4.5V19a1.5 1.5 0 0 1-1.5 1.5h-14A1.5 1.5 0 0 1 3.5 19z"/>'
        '<path d="M12 3.5v9M8.5 9l3.5 3.5L15.5 9"/>'
    ),
    "upload": (
        f'<path d="M3.5 15v4a1.5 1.5 0 0 0 1.5 1.5h14a1.5 1.5 0 0 0 1.5-1.5v-4" {_DUO}/>'
        '<path d="M3.5 15v4a1.5 1.5 0 0 0 1.5 1.5h14a1.5 1.5 0 0 0 1.5-1.5v-4"/>'
        '<path d="M12 15.5V4M7.5 8.5L12 4l4.5 4.5"/>'
    ),
    "save": (
        f'<rect x="7.5" y="13" width="9" height="7.5" {_DUO}/>'
        '<path d="M4 5.5a2 2 0 0 1 2-2h10l4 4V18.5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2z"/>'
        '<path d="M7.5 20.5V13h9v7.5M8 3.5v4h7v-4"/>'
    ),
    "trash": (
        f'<path d="M6 7l1 13.5h10L18 7z" {_DUO}/>'
        '<path d="M4 7h16M9.5 7V4.5h5V7M6 7l1 13.5h10L18 7"/><path d="M10 11v6M14 11v6"/>'
    ),
    "flag": (
        f'<path d="M5.5 4h13v9h-13z" {_DUO}/>'
        '<path d="M5.5 21V4h13v9h-13"/>'
        '<path d="M5.5 4h3.3v3h-3.3zM12 4h3.2v3H12zM8.8 7H12v3H8.8zM15.2 7h3.3v3h-3.3zM5.5 10h3.3v3H5.5zM12 10h3.2v3H12z" '
        'fill="currentColor" stroke="none"/>'
    ),

    # --- 操作 ---------------------------------------------------------------
    "search": (
        f'<circle cx="10.5" cy="10.5" r="6" {_DUO}/>'
        '<circle cx="10.5" cy="10.5" r="6"/><path d="M15 15l5.5 5.5"/>'
    ),
    "refresh": (
        '<path d="M19.5 12a7.5 7.5 0 0 1-13.2 4.9M4.5 12a7.5 7.5 0 0 1 13.2-4.9"/>'
        '<path d="M18 3.5v3.6h-3.6M6 20.5v-3.6h3.6"/>'
    ),
    "link": (
        '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/>'
        '<path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>'
    ),
    "plus": (
        f'<circle cx="12" cy="12" r="8.5" {_DUO}/>'
        '<circle cx="12" cy="12" r="8.5"/><path d="M12 8v8M8 12h8"/>'
    ),
    "check": (
        f'<circle cx="12" cy="12" r="8.5" {_DUO}/>'
        '<circle cx="12" cy="12" r="8.5"/><path d="M8 12.3l2.7 2.7L16 9.5"/>'
    ),
    "close": '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
    "warning": (
        f'<path d="M10.3 4.4L2.8 17.5A2 2 0 0 0 4.5 20.5h15a2 2 0 0 0 1.7-3L13.7 4.4a2 2 0 0 0-3.4 0z" {_DUO}/>'
        '<path d="M10.3 4.4L2.8 17.5A2 2 0 0 0 4.5 20.5h15a2 2 0 0 0 1.7-3L13.7 4.4a2 2 0 0 0-3.4 0z"/>'
        '<path d="M12 9.5v4.5M12 17h.01"/>'
    ),
    "wrench": (
        f'<path d="M14.5 3.8a4.5 4.5 0 0 0-4.3 6.1l-6.3 6.3a2 2 0 0 0 2.9 2.9l6.3-6.3a4.5 4.5 0 0 0 6.1-4.3l-2.7 2.7-2.9-.8-.8-2.9z" {_DUO}/>'
        '<path d="M14.5 3.8a4.5 4.5 0 0 0-4.3 6.1l-6.3 6.3a2 2 0 0 0 2.9 2.9l6.3-6.3a4.5 4.5 0 0 0 6.1-4.3l-2.7 2.7-2.9-.8-.8-2.9z"/>'
    ),
    "more": '<path d="M6 12h.01M12 12h.01M18 12h.01" stroke-width="3"/>',
    "chev_right": '<path d="M9.5 6l6 6-6 6"/>',
    "chev_left": '<path d="M14.5 6l-6 6 6 6"/>',

    # --- アカウント ---------------------------------------------------------
    "user": (
        f'<circle cx="12" cy="8.5" r="4" {_DUO}/>'
        '<circle cx="12" cy="8.5" r="4"/><path d="M4.5 20.5a7.5 7.5 0 0 1 15 0"/>'
    ),
    "users": (
        f'<circle cx="9" cy="8.5" r="3.5" {_DUO}/>'
        '<circle cx="9" cy="8.5" r="3.5"/><path d="M2.5 20a6.5 6.5 0 0 1 13 0"/>'
        '<path d="M15 5.2a3.5 3.5 0 0 1 0 6.6M17.5 14.3A6.5 6.5 0 0 1 21.5 20"/>'
    ),
    "key": (
        f'<circle cx="8" cy="15.5" r="4.5" {_DUO}/>'
        '<circle cx="8" cy="15.5" r="4.5"/><path d="M11.2 12.3L20 3.5M16.5 7l2.5 2.5M14 9.5l2 2"/>'
    ),
    "lock": (
        f'<rect x="5" y="10.5" width="14" height="10" rx="2" {_DUO}/>'
        '<rect x="5" y="10.5" width="14" height="10" rx="2"/>'
        '<path d="M8 10.5V7.5a4 4 0 0 1 8 0v3M12 14.5v2.5"/>'
    ),
    "logout": (
        f'<path d="M4 4.5a1 1 0 0 1 1-1h8v17H5a1 1 0 0 1-1-1z" {_DUO}/>'
        '<path d="M13 3.5H5a1 1 0 0 0-1 1v15a1 1 0 0 0 1 1h8"/>'
        '<path d="M10 12h10.5M17 8.5l3.5 3.5-3.5 3.5"/>'
    ),
}


def icon(name, cls=""):
    """
    アイコンをインラインSVGで返す。大きさは文字サイズ(1em)に合わせる。
    知らない名前はそのまま文字で出す（絵文字が残っていても表示が消えないように）。
    """
    body = ICONS.get(name)
    if body is None:
        return escape(name or "")
    return Markup(
        f'<svg class="ico {cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        f'stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{body}</svg>'
    )


# アイコンバッジの背景色 → アイコンの色。絵文字は自前の色を持っていたが、線画は単色なので
# 背景と同じ色相の濃い色で塗り、カードごとの色分けを保つ
_TINTS = {
    "bg-amber-50": "text-amber-600",
    "bg-blue-50": "text-blue-600",
    "bg-emerald-50": "text-emerald-600",
    "bg-rose-50": "text-rose-600",
    "bg-pink-50": "text-pink-600",
}


def icon_tint(icon_bg):
    return _TINTS.get(icon_bg, "text-gray-700")
