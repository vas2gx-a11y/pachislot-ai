import base64
import os
import re
import secrets
import tempfile
import time
from datetime import datetime

from flask import Blueprint, render_template, request, redirect, url_for, flash

import common

store_trends_bp = Blueprint("store_trends", __name__, url_prefix="/store_trends")

# 集計対象期間の選択肢(ラベル, 日数)。0は全期間。
PERIOD_CHOICES = [("直近30日", 30), ("直近90日", 90), ("直近1年", 365), ("全期間", 0)]
DEFAULT_DAYS = 90


# 取り込みファイルとして受け付ける拡張子とサイズ上限
IMPORT_TEXT_EXTENSIONS = {"txt", "csv", "tsv"}
MAX_IMPORT_FILE_SIZE = 4 * 1024 * 1024  # 4MB
# 1回の取り込みで受け付ける日数の上限(データ一覧は5年分=1800日近くになる)
MAX_IMPORT_ROWS = 4000
# 台別データは1日分でも大型店だと1000台を超える
MAX_IMPORT_UNIT_ROWS = 3000


def _read_uploaded_text(file):
    """
    アップロードされたテキストファイルを文字列にする。
    戻り値: (テキスト, エラーメッセージ)。エラーメッセージが空なら成功。
    """
    filename = (file.filename or "").lower()
    ext = filename.rsplit(".", 1)[-1] if "." in filename else ""
    if ext not in IMPORT_TEXT_EXTENSIONS:
        return "", "対応していないファイル形式です(txt / csv / tsv のみ)"

    raw = file.read()
    if not raw:
        return "", "ファイルの中身が空です。"
    if len(raw) > MAX_IMPORT_FILE_SIZE:
        return "", "ファイルが大きすぎます(4MBまで)。期間を分けて取り込んでください。"

    # Excelで保存したCSVなどはUTF-8とは限らないため、日本語で使われる文字コードを順に試す
    for encoding in ("utf-8-sig", "cp932", "utf-16"):
        try:
            return raw.decode(encoding), ""
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="ignore"), ""


# 取り込み待ちファイルの一時置き場。
# CSVは数万行になることがあり、確認画面のhiddenフィールドで持ち回すには大きすぎるため、
# 解析結果を確認している間だけサーバー側に置いて、トークンで参照する。
_UPLOAD_DIR = os.path.join(tempfile.gettempdir(), "pachislot_imports")
_UPLOAD_TTL_SECONDS = 60 * 60


def _stash_upload(text):
    """取り込み待ちのテキストを一時保存して、参照用トークンを返す"""
    os.makedirs(_UPLOAD_DIR, exist_ok=True)
    _cleanup_uploads()
    token = secrets.token_hex(16)
    with open(os.path.join(_UPLOAD_DIR, f"{token}.txt"), "w", encoding="utf-8") as f:
        f.write(text)
    return token


def _read_stash(token):
    """トークンで一時保存したテキストを読む(見つからなければ空文字)"""
    if not token or not re.fullmatch(r"[0-9a-f]{32}", token):
        return ""
    path = os.path.join(_UPLOAD_DIR, f"{token}.txt")
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as f:
        return f.read()


def _drop_stash(token):
    if token and re.fullmatch(r"[0-9a-f]{32}", token):
        try:
            os.remove(os.path.join(_UPLOAD_DIR, f"{token}.txt"))
        except OSError:
            pass


def _cleanup_uploads():
    """確認せずに離脱した分が残り続けないよう、古い一時ファイルを消す"""
    limit = time.time() - _UPLOAD_TTL_SECONDS
    try:
        for name in os.listdir(_UPLOAD_DIR):
            path = os.path.join(_UPLOAD_DIR, name)
            if os.path.isfile(path) and os.path.getmtime(path) < limit:
                os.remove(path)
    except OSError:
        pass


def _parse_days(raw):
    """期間パラメータを検証する(想定外の値が来たら既定値に戻す)"""
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    return days if days in {d for _, d in PERIOD_CHOICES} else DEFAULT_DAYS


def _render(store_name, days, ai_summary="", import_preview=None, pasted_text="",
            unit_import_preview=None, pasted_units_text="", unit_date="",
            csv_import_preview=None):
    stores = common.list_store_names()

    # 店舗が未指定なら、記録が一番多い店舗を初期表示にする(毎回選び直す手間を省く)
    if not store_name and stores:
        store_name = stores[0]["name"]

    trends = common.build_store_trends(store_name, days=days) if store_name else None
    store_stats = common.load_store_stats().get(store_name) if store_name else None
    # 取り込んだホールデータ(店の全台)は、自分の記録より対象期間が長いことが多いので
    # 画面の期間指定とは別に、常に直近1年分を集計する
    daily_trends = common.build_store_daily_trends(store_name, days=365) if store_name else None
    # 台別データは台ごとの傾向を見るためのものなので、画面の期間指定に合わせて集計する
    unit_trends = common.build_store_unit_trends(store_name, days=days) if store_name else None

    return render_template(
        "store_trends.html",
        stores=stores,
        store_name=store_name,
        days=days,
        period_choices=PERIOD_CHOICES,
        trends=trends,
        store_stats=store_stats,
        daily_trends=daily_trends,
        unit_trends=unit_trends,
        import_preview=import_preview,
        pasted_text=pasted_text,
        unit_import_preview=unit_import_preview,
        pasted_units_text=pasted_units_text,
        csv_import_preview=csv_import_preview,
        unit_date=unit_date or datetime.now().strftime("%Y-%m-%d"),
        ai_summary=ai_summary,
    )


def _back_to(store_name, days):
    return redirect(url_for("store_trends.index", store_name=store_name, days=days))


@store_trends_bp.route("/")
def index():
    store_name = request.args.get("store_name", "").strip()
    days = _parse_days(request.args.get("days", DEFAULT_DAYS))
    return _render(store_name, days)


@store_trends_bp.route("/ai_summary", methods=["POST"])
def ai_summary():
    """集計結果をAIに総評してもらう(ボタンを押したときだけAPIを呼ぶ)"""
    store_name = request.form.get("store_name", "").strip()
    days = _parse_days(request.form.get("days", DEFAULT_DAYS))

    trends = common.build_store_trends(store_name, days=days) if store_name else None
    if not trends or not trends.get("record_count"):
        flash("この条件では集計できる記録がないため、AI総評を作成できません。")
        return _render(store_name, days)

    store_stats = common.load_store_stats().get(store_name)
    daily_trends = common.build_store_daily_trends(store_name, days=365)
    unit_trends = common.build_store_unit_trends(store_name, days=days)
    summary, error = common.summarize_store_trends_with_gemini(
        trends, store_stats=store_stats, daily_trends=daily_trends, unit_trends=unit_trends
    )
    if error:
        flash(error)
    return _render(store_name, days, ai_summary=summary)


@store_trends_bp.route("/upload_stats", methods=["POST"])
def upload_stats():
    """
    データサイト等のスクショから、店舗の年間データ(総差枚・平均差枚・平均G数・勝率)を
    AIに読み取らせて保存する。

    どの店舗のデータとして保存するかは、画面で選択中の店舗名を正とする。
    (画像から読み取った店名は表記ゆれが起きやすく、記録側の店舗名と一致しないと
    分析で紐づかないため。画像の店名が違って見える場合は警告だけ出す)
    """
    store_name = request.form.get("store_name", "").strip()
    days = _parse_days(request.form.get("days", DEFAULT_DAYS))
    file = request.files.get("stats_image")

    if not file or file.filename == "":
        flash("年間データの画像を選択してください。")
        return _back_to(store_name, days)

    if not common.allowed_file(file.filename):
        flash("対応していないファイル形式です(jpg / jpeg / png / webp のみ)")
        return _back_to(store_name, days)

    ext = file.filename.rsplit(".", 1)[1].lower()
    mime_type = "image/png" if ext == "png" else "image/webp" if ext == "webp" else "image/jpeg"
    base64_image = base64.b64encode(file.read()).decode("utf-8")

    parsed = common.analyze_store_stats_image_with_gemini(base64_image, mime_type)
    if not parsed:
        flash("画像の解析に失敗しました。もう一度お試しいただくか、下の入力欄に手入力してください。")
        return _back_to(store_name, days)

    # 画面で店舗が選ばれていない場合に限り、画像から読み取った店名を使う
    target_store = store_name or parsed.get("store_name", "").strip()
    if not target_store:
        flash("保存先の店舗が特定できませんでした。店舗を選んでから、もう一度お試しください。")
        return _back_to(store_name, days)

    ai_store_name = parsed.get("store_name", "").strip()
    if ai_store_name and store_name and ai_store_name != store_name:
        flash(f"画像から読み取った店名「{ai_store_name}」は選択中の「{store_name}」と異なります。"
              f"選択中の店舗のデータとして保存しました。違う場合は店舗を選び直してください。")

    read_values = [k for k in ("total_diff", "avg_diff", "avg_games", "win_rate") if parsed.get(k) is not None]
    if not read_values:
        flash("画像から数値を読み取れませんでした。下の入力欄に手入力してください。")
        return _back_to(target_store, days)

    ok, message = common.save_store_stats(
        target_store,
        period_label=parsed.get("period_label", ""),
        total_diff=parsed.get("total_diff"),
        avg_diff=parsed.get("avg_diff"),
        avg_games=parsed.get("avg_games"),
        win_rate=parsed.get("win_rate"),
        note=parsed.get("note", ""),
        source="画像から読み取り",
    )
    flash(message if ok else message)
    if ok and len(read_values) < 4:
        missing = {"total_diff": "総差枚", "avg_diff": "平均差枚", "avg_games": "平均G数", "win_rate": "勝率"}
        not_read = [label for key, label in missing.items() if key not in read_values]
        flash(f"画像から読み取れなかった項目({'、'.join(not_read)})があります。下の入力欄で補ってください。")
    return _back_to(target_store, days)


@store_trends_bp.route("/save_stats", methods=["POST"])
def save_stats():
    """AIの読み取り結果の修正・手入力での保存"""
    store_name = request.form.get("store_name", "").strip()
    days = _parse_days(request.form.get("days", DEFAULT_DAYS))

    if not store_name:
        flash("店舗を選んでから保存してください。")
        return _back_to(store_name, days)

    ok, message = common.save_store_stats(
        store_name,
        period_label=request.form.get("period_label", ""),
        total_diff=common.parse_number(request.form.get("total_diff")),
        avg_diff=common.parse_number(request.form.get("avg_diff")),
        avg_games=common.parse_number(request.form.get("avg_games")),
        win_rate=common.parse_number(request.form.get("win_rate")),
        note=request.form.get("note", ""),
        source="手入力",
    )
    flash(message)
    return _back_to(store_name, days)


@store_trends_bp.route("/import_daily", methods=["POST"])
def import_daily():
    """
    ホールデータサイトの一覧表をコピーして貼り付けたテキストを解析し、
    店舗の日別データとして一括登録する。

    いきなり保存すると、貼り付け形式が想定と違ったときに変な値がシートに入ってしまうため、
    1回目は解析結果のプレビューを返し、内容を確認してから保存する2段階にしている。
    """
    store_name = request.form.get("store_name", "").strip()
    days = _parse_days(request.form.get("days", DEFAULT_DAYS))
    pasted_text = request.form.get("pasted_text", "")
    confirmed = request.form.get("confirm") == "1"

    if not store_name:
        flash("取り込み先の店舗を選んでください。")
        return _render(store_name, days)

    # ファイルが選ばれていればそちらを優先する。
    # データ一覧をまるごと取り込むと1000日分を超えることがあり、
    # その量を貼り付け欄に入れるのは現実的でないため。
    uploaded = request.files.get("daily_file")
    if uploaded and uploaded.filename:
        text, error = _read_uploaded_text(uploaded)
        if error:
            flash(error)
            return _render(store_name, days, pasted_text=pasted_text)
        pasted_text = text

    if not pasted_text.strip():
        flash("ホールデータの表を貼り付けるか、テキストファイルを選んでください。")
        return _render(store_name, days)

    rows, report = common.parse_hall_daily_text(pasted_text, max_rows=MAX_IMPORT_ROWS)
    if not rows:
        flash("貼り付けたテキストから日別データを読み取れませんでした。"
              "日付・総差枚・平均差枚・平均G数・勝率が並んだ表をそのままコピーしてください。")
        return _render(store_name, days, pasted_text=pasted_text)

    if not confirmed:
        # プレビュー(まだ保存しない)
        return _render(store_name, days, pasted_text=pasted_text,
                       import_preview={"rows": rows[:10], "report": report, "total": len(rows)})

    ok, message, _counts = common.save_store_daily_rows(store_name, rows)
    flash(message)
    if ok and report["skipped"]:
        flash(f"列が足りずに読み飛ばした行が{report['skipped']}件あります。"
              f"表の一部だけをコピーしていないか確認してください。")
    return _back_to(store_name, days)


@store_trends_bp.route("/import_csv", methods=["POST"])
def import_csv():
    """
    ホールデータ取り込みツール(tools/anaslo.py)が出すCSVを取り込む。

    このCSVは1行が「1日×1台」なので、1つのファイルから
    台別データ(store_units)と日別データ(store_daily)の両方を作れる。
    複数日がまとまっていても、まとめて取り込める。

    行数が多いため確認画面ではファイルを持ち回さず、サーバー側に一時保存して
    トークンで参照する(貼り付け取り込みと違い、hiddenフィールドには収まらないため)。
    """
    store_name = request.form.get("store_name", "").strip()
    days = _parse_days(request.form.get("days", DEFAULT_DAYS))
    confirmed = request.form.get("confirm") == "1"
    token = request.form.get("upload_token", "").strip()

    if not store_name:
        flash("取り込み先の店舗を選んでください。")
        return _render(store_name, days)

    if confirmed:
        text = _read_stash(token)
        if not text:
            flash("確認中のファイルが見つかりませんでした(時間が経つと破棄されます)。"
                  "もう一度ファイルを選んでください。")
            return _render(store_name, days)
    else:
        uploaded = request.files.get("csv_file")
        if not uploaded or not uploaded.filename:
            flash("取り込むCSVファイルを選んでください。")
            return _render(store_name, days)
        text, error = _read_uploaded_text(uploaded)
        if error:
            flash(error)
            return _render(store_name, days)

    units_by_date, daily_rows, report = common.parse_anaslo_csv_text(text)
    if not units_by_date:
        _drop_stash(token)
        flash("CSVから台別データを読み取れませんでした。"
              "date / machine_number の列があるか確認してください"
              "(tools/anaslo.py の parse で作ったCSVをそのまま選んでください)。")
        return _render(store_name, days)

    # 店舗の取り違えは後から直しにくいので、CSVの店名が選択中と違えば知らせる
    csv_stores = [n for n in report.get("store_names", []) if n and n != store_name]
    if csv_stores:
        flash(f"CSVに入っている店名「{'、'.join(csv_stores)}」は選択中の「{store_name}」と違います。"
              f"選択中の店舗のデータとして扱います。")

    if not confirmed:
        new_token = _stash_upload(text)
        return _render(store_name, days, csv_import_preview={
            "report": report, "daily": daily_rows[:8], "token": new_token,
        })

    ok_units, message_units, counts = common.save_store_unit_rows_multi(
        store_name, units_by_date, source="CSV取り込み"
    )
    flash(message_units)
    if ok_units:
        ok_daily, message_daily, _ = common.save_store_daily_rows(
            store_name, daily_rows, source="CSV取り込み(台別データから計算)"
        )
        flash(message_daily)
    _drop_stash(token)
    return _back_to(store_name, days)


@store_trends_bp.route("/import_units", methods=["POST"])
def import_units():
    """
    日別詳細ページ(機種・台番号ごとのデータ)をコピーして貼り付けたテキストを解析し、
    その日の台別データとして一括登録する。

    列の並びはサイトによって違うため、貼り付けに見出し行が含まれていれば
    そこから列を判定する。日別データと同じく、保存前にプレビューで確認する。
    """
    store_name = request.form.get("store_name", "").strip()
    days = _parse_days(request.form.get("days", DEFAULT_DAYS))
    unit_date = request.form.get("unit_date", "").strip()
    pasted_units_text = request.form.get("pasted_units_text", "")
    confirmed = request.form.get("confirm") == "1"

    if not store_name:
        flash("取り込み先の店舗を選んでください。")
        return _render(store_name, days)

    try:
        datetime.strptime(unit_date, "%Y-%m-%d")
    except ValueError:
        flash("台別データは日付ごとのデータなので、対象の日付を YYYY-MM-DD 形式で指定してください。")
        return _render(store_name, days, pasted_units_text=pasted_units_text, unit_date=unit_date)

    # 日別データと同じく、ファイルが選ばれていればそちらを優先する
    # (1日分でも1000台を超える店があり、貼り付け欄では扱いきれないため)
    uploaded = request.files.get("units_file")
    if uploaded and uploaded.filename:
        text, error = _read_uploaded_text(uploaded)
        if error:
            flash(error)
            return _render(store_name, days, pasted_units_text=pasted_units_text, unit_date=unit_date)
        pasted_units_text = text

    if not pasted_units_text.strip():
        flash("台別データの表を貼り付けるか、テキストファイルを選んでください。")
        return _render(store_name, days, unit_date=unit_date)

    rows, report = common.parse_hall_unit_text(pasted_units_text, max_rows=MAX_IMPORT_UNIT_ROWS)
    if not rows:
        flash("貼り付けたテキストから台別データを読み取れませんでした。"
              "台番号・G数・差枚などが並んだ表を、見出し行を含めてコピーしてください。")
        return _render(store_name, days, pasted_units_text=pasted_units_text, unit_date=unit_date)

    if not confirmed:
        return _render(store_name, days, pasted_units_text=pasted_units_text, unit_date=unit_date,
                       unit_import_preview={"rows": rows[:12], "report": report,
                                            "total": len(rows), "date": unit_date})

    ok, message, _counts = common.save_store_unit_rows(store_name, unit_date, rows)
    flash(message)
    if ok and report["skipped"]:
        flash(f"読み取れずに飛ばした行が{report['skipped']}件あります。"
              f"見出し行を含めてコピーすると、列の対応をより正確に判定できます。")
    return _back_to(store_name, days)
