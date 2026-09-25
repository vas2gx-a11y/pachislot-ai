#!/usr/bin/env python3
"""
店舗情報(store_data/<id>.json)をWebページから集めて更新するツール。

流れ:
    ページ取得 → 本文テキスト化 → Geminiで項目ごとに抽出 → 根拠の照合 → 差分表示 → (--apply で)保存

使い方:
    # 1. 店舗を登録(店舗名＋地域だけの空のJSONができる)
    python3 tools/store_collect.py new bellagio_nishinakajima "ベラジオ西中島店" --region 大阪府

    # 2. 公式サイトなどから集める(既定は差分を見せるだけで保存しない)
    python3 tools/store_collect.py collect bellagio_nishinakajima --url https://example.com/ --reliability 公式

    # 3. 差分が正しければ保存(変更前・変更後が history に残る)
    python3 tools/store_collect.py collect bellagio_nishinakajima --url https://example.com/ --reliability 公式 --apply

    # Cloudflare等で requests が弾かれるページは、ブラウザで保存したHTML/テキストを渡す
    python3 tools/store_collect.py collect bellagio_nishinakajima --url https://example.com/ --file page.html

    # 登録済みの全店舗を、前回使った出典URLから取り直す(定期実行用)
    python3 tools/store_collect.py refresh --apply

    # チャット(Claude)で調べてもらった収集結果JSONを反映する。店舗が無ければ作る
    python3 tools/store_collect.py apply data/store_collected/bellagio_nishinakajima_2026-09-25.json --apply

【推測で埋めないための仕組み】
AIには値と一緒に「ページ本文からそのまま抜き出した根拠(evidence)」を返させ、
その文字列が本文に実在しない値は捨てる。確認状態(確認済/未確認)もAIには決めさせず、
出典の種類(--reliability)からコード側で決める。

GEMINI_API_KEY が必要(common.py の設定をそのまま使う)。
"""

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import store_info  # noqa: E402

# 店舗ページ1枚の抽出にかかる時間の上限(秒)。出力が長めなので common の既定より長くとる
GEMINI_TIMEOUT = 180

# 抽出の形をAIに伝えるための説明。store_info.FIELDS の kind と対応させている。
KIND_HINTS = {
    "text": '"文字列"',
    "int": "整数",
    "list": '["文字列", ...]',
    "links": '[{"label": "サービス名", "url": "https://..."}]',
    "machines": '[{"name": "機種名", "units": 台数の整数またはnull}]',
    "dated": '[{"date": "YYYY-MM-DD またはnull", "title": "見出し", "detail": "本文"}]',
}


def _common():
    """
    ページ取得とGeminiの呼び出しは common.py のものを使う(機種URL取り込みと挙動を揃えるため)。
    common.py は読み込んだ時点でシートの設定も必須にしているが、このツールはシートに触らないので、
    未設定なら使われない値を入れて読み込ませる(GEMINI_API_KEY だけは本物が要る)。
    """
    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit("GEMINI_API_KEY が設定されていません。")
    os.environ.setdefault("SPREADSHEET_ID", "unused-by-store-collect")
    os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
    import common
    return common


def _page_text(url, file_path):
    common = _common()

    if file_path:
        with open(file_path, encoding="utf-8", errors="ignore") as f:
            raw = f.read()
        if "<html" in raw[:2000].lower() or "<body" in raw.lower():
            parser = common._VisibleTextExtractor()
            parser.feed(raw)
            raw = parser.get_text()
        text = re.sub(r"\n{2,}", "\n", raw).strip()
        return text[:common.URL_TEXT_MAX_CHARS]
    return common.fetch_url_text(url)


def _build_prompt(store, page_text, url):
    fields = "\n".join(
        f'  "{key}": {{"value": {KIND_HINTS[kind]} または null, "evidence": "根拠の原文"}},   // {label}'
        for key, label, kind in store_info.FIELDS
    )
    return f"""
以下はパチンコ・パチスロ店「{store['name']}」（{store['region']}）に関するWebページの本文です
（HTMLからテキストだけを抜き出したもの）。この店舗の情報を、下のJSON形式でのみ出力してください。

厳守すること:
- ページに書かれていない情報は必ず null にする。一般的な知識や他店舗の情報で補わない。
- evidence には、その値の根拠になるページ本文の一部を**一字一句そのまま**抜き出す（30〜80文字程度）。
  要約・言い換えはしない。根拠が示せない値は null にする。
- 別の店舗（系列店・近隣店）の情報が混ざっている場合は、「{store['name']}」のものだけを使う。
- ページが別の店舗のものなら、すべて null にし "same_store": false にする。
- 台数・料金などの数字はページの表記どおりに。単位の換算や合算で作った値は入れない。

{{
  "same_store": true,
{fields}
}}

【ページURL】{url or "不明"}
【ページ本文】
{page_text}
"""


def _call_gemini(prompt):
    import requests

    common = _common()

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            # 抽出なので揺らぎは要らない。JSONで返させて前後の文章を除く手間を省く
            "temperature": 0,
            "responseMimeType": "application/json",
            # 2.5系は既定で答える前に推論を挟み、店舗ページ1枚で60秒を超えることがあった。
            # 書いてあることを抜き出すだけなので推論は切る
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    try:
        res = requests.post(common.GEMINI_URL, json=payload, timeout=GEMINI_TIMEOUT)
        res.raise_for_status()
    except requests.exceptions.Timeout:
        raise SystemExit(f"Geminiの応答が{GEMINI_TIMEOUT}秒以内に返りませんでした。時間をおいて再実行してください。")
    except requests.exceptions.HTTPError as e:
        # キーの誤りなどはここに来る。本文にエラーの理由が入っているので出す(URLにはキーが入るので出さない)
        raise SystemExit(f"Gemini APIエラー {e.response.status_code}: {e.response.text[:300]}")
    raw = res.json()["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(raw[raw.find("{"): raw.rfind("}") + 1])


def _squash(s):
    return re.sub(r"\s+", "", str(s or ""))


def extract(store, page_text, url):
    """
    ページから項目を抜き出す。戻り値は (values, rejected)。
    rejected は根拠がページに見つからず捨てた項目(AIが作った値の可能性があるもの)。
    """
    result = _call_gemini(_build_prompt(store, page_text, url))
    if result.get("same_store") is False:
        raise SystemExit(f"このページは「{store['name']}」のものではないと判定されました: {url}")

    body = _squash(page_text)
    values, rejected = {}, []
    for key in store_info.FIELD_KEYS:
        item = result.get(key) or {}
        v = item.get("value")
        if store_info._is_empty(v):
            continue
        evidence = _squash(item.get("evidence"))
        if not evidence or evidence not in body:
            rejected.append((key, v, item.get("evidence")))
            continue
        if not store_info._kind_ok(store_info.FIELD_KINDS[key], v):
            rejected.append((key, v, "値の形が定義と違う"))
            continue
        values[key] = v
    return values, rejected


def _fmt(v):
    if v is None:
        return "（不明）"
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)


def _load_or_exit(store_id):
    store = store_info.load(store_id)
    if store is None:
        raise SystemExit(f"store_data/{store_id}.json がありません。先に new で登録してください。")
    return store


def collect_one(store, url, file_path, source_name, reliability, apply):
    text = _page_text(url, file_path)
    if not text:
        print(f"  ページを取得できませんでした: {url}（Cloudflare等で弾かれる場合は --file で渡す）")
        return False

    values, rejected = extract(store, text, url)
    return _report_and_save(store, values, rejected, url, source_name, reliability, apply)


def _report_and_save(store, values, rejected, url, source_name, reliability, apply, checked_at=None):
    """差分を表示し、apply なら店舗JSONに重ねて保存する。collect と apply で共通。"""
    changes = store_info.diff(store, values, reliability)
    conflicts = [c for c in changes if c["conflict"]]
    changes = [c for c in changes if not c["conflict"]]

    print(f"■ {store['name']}  ← {url}")
    print(f"  読み取れた項目 {len(values)} / 変更 {len(changes)}")
    for c in changes:
        label = store_info.FIELD_LABELS[c["field"]]
        print(f"  ・{label}\n      前: {_fmt(c['before'])}\n      後: {_fmt(c['after'])}")
    for c in conflicts:
        label = store_info.FIELD_LABELS[c["field"]]
        print(f"  △ 公式と食い違い（反映しない）: {label} = {_fmt(c['after'])}　公式: {_fmt(c['before'])}")
    for key, v, why in rejected:
        print(f"  × 捨てた: {store_info.FIELD_LABELS[key]} = {_fmt(v)}（根拠がページに無い: {why}）")

    if not apply:
        print("  （確認のみ。保存するには --apply）")
        return bool(changes)

    sid = store_info.upsert_source(store, url, source_name, reliability, checked_at)
    store_info.merge(store, values, sid, reliability)
    problems = store_info.validate(store, store["id"])
    store_info.save(store)
    print(f"  保存しました: store_data/{store['id']}.json")
    for p in problems:
        print(f"  ⚠ {p}")
    return bool(changes)


def cmd_new(args):
    if store_info.load(args.store_id) is not None:
        raise SystemExit(f"store_data/{args.store_id}.json はもうあります。")
    store = store_info.new_store(args.store_id, args.name, args.region, args.sheet_name)
    print(f"作成しました: {store_info.save(store)}")


def cmd_collect(args):
    store = _load_or_exit(args.store_id)
    collect_one(store, args.url, args.file, args.source_name, args.reliability, args.apply)


def cmd_refresh(args):
    """
    登録済みの出典URLを取り直して差分を出す。定期実行(cron・n8n など)から呼ぶ想定。
    --file で渡した出典(ブラウザでしか取れないページ)は requests で取れないことがあるが、
    取れなければその出典を飛ばすだけにしている。
    """
    ids = [args.store_id] if args.store_id else store_info.store_ids()
    for sid in ids:
        store = _load_or_exit(sid)
        if not store.get("sources"):
            print(f"■ {store['name']}: 出典が未登録なので飛ばします（collect で最初の出典を登録）")
            continue
        for s in list(store["sources"]):
            if s.get("url"):
                collect_one(store, s["url"], None, s.get("name"), s.get("reliability"), args.apply)


def _values_from_payload(values):
    """
    収集結果JSONの values を {項目: 値} にする。戻り値は (values, rejected)。
    根拠(evidence)の無い値は、Geminiで集めたときと同じく推測と区別できないので捨てる。
    """
    out, rejected = {}, []
    for key, item in (values or {}).items():
        if key not in store_info.FIELD_KINDS:
            raise SystemExit(f"未定義の項目です: {key}（store_info.FIELDS を参照）")
        v = item.get("value") if isinstance(item, dict) else None
        if store_info._is_empty(v):
            continue
        if not str(item.get("evidence") or "").strip():
            rejected.append((key, v, "evidence が空"))
            continue
        if not store_info._kind_ok(store_info.FIELD_KINDS[key], v):
            rejected.append((key, v, "値の形が定義と違う"))
            continue
        out[key] = v
    return out, rejected


def cmd_apply(args):
    """
    チャット等で集めた収集結果JSONを反映する(書式は store_data/README.md)。
    店舗がまだ無ければ、収集結果の name / region から作る(「店舗名＋地域」だけで登録できるように)。
    """
    with open(args.file, encoding="utf-8") as f:
        payload = json.load(f)
    store_id = payload.get("store_id")
    store = store_info.load(store_id)
    if store is None:
        if not payload.get("name") or not payload.get("region"):
            raise SystemExit(f"store_data/{store_id}.json が無いので、収集結果に name と region が必要です。")
        store = store_info.new_store(store_id, payload["name"], payload["region"],
                                     payload.get("sheet_store_name", ""))
        print(f"新しい店舗として登録します: {payload['name']}（{payload['region']}）")
        if store_info._path_of(store_id) is None:
            raise SystemExit(f"店舗IDが不正です: {store_id!r}（英小文字・数字・_ のみ）")

    for src in payload.get("sources") or []:
        if src.get("reliability") not in store_info.RELIABILITIES:
            raise SystemExit(f"reliability は {' / '.join(store_info.RELIABILITIES)} のいずれか: {src.get('url')}")
        values, rejected = _values_from_payload(src.get("values"))
        _report_and_save(store, values, rejected, src.get("url"), src.get("name"),
                         src["reliability"], args.apply, src.get("checked_at"))
        if not args.apply:
            # 確認だけのときも、次の出典との比較が保存時と同じになるようメモリ上では重ねておく
            # (公式の後にポータルを重ねたときの「食い違い」が確認の段階で見えるように)
            sid = store_info.upsert_source(store, src.get("url"), src.get("name"),
                                           src["reliability"], src.get("checked_at"))
            store_info.merge(store, values, sid, src["reliability"])


def main():
    p = argparse.ArgumentParser(description="店舗情報をWebから集めて store_data/ を更新する")
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("new", help="店舗名＋地域で空の店舗を登録")
    n.add_argument("store_id", help="英小文字・数字・_（ファイル名になる）")
    n.add_argument("name", help="店舗名")
    n.add_argument("--region", required=True, help="都道府県")
    n.add_argument("--sheet-name", default="", help="シート側の店舗名が違う場合だけ指定")
    n.set_defaults(func=cmd_new)

    c = sub.add_parser("collect", help="1ページから集める")
    c.add_argument("store_id")
    c.add_argument("--url", required=True, help="情報源のURL（--file のときも出典として保存する）")
    c.add_argument("--file", help="ブラウザで保存したHTML/テキスト")
    c.add_argument("--source-name", default="", help="出典名（省略時はURL）")
    c.add_argument("--reliability", choices=store_info.RELIABILITIES, default="その他")
    c.add_argument("--apply", action="store_true", help="差分を保存する")
    c.set_defaults(func=cmd_collect)

    r = sub.add_parser("refresh", help="登録済みの出典を取り直す")
    r.add_argument("store_id", nargs="?", help="省略時は全店舗")
    r.add_argument("--apply", action="store_true")
    r.set_defaults(func=cmd_refresh)

    a = sub.add_parser("apply", help="収集結果JSON（チャットで集めたもの）を反映する")
    a.add_argument("file", help="data/store_collected/<id>_<日付>.json など")
    a.add_argument("--apply", action="store_true", help="差分を保存する")
    a.set_defaults(func=cmd_apply)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
