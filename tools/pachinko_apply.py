"""
調べたパチンコの機種データ(JSON)を pachinko_machines シートに反映する。

    SPREADSHEET_ID=... GOOGLE_SERVICE_ACCOUNT_JSON="$(cat key.json)" \
        python3 tools/pachinko_apply.py data/pachinko_collected/*.json

画面の登録フォームと同じ保存処理(common.save_pachinko_machine)を通すので、中身は画面から入れたものと変わらない。
チャットで機種の登録を頼まれたとき、Webで調べた結果を1件ずつフォームに打ち直さずに済むように作った。
同じ機種名がすでにあればその行を上書きする(調べ直して反映し直せるように)。

JSONのキーは common.PACHINKO_HEADERS と同じ(machine_id と updated_at は不要)。
effects は [{"name": 演出名, "rate": 期待度%, "note": 補足}] の配列。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(paths):
    if not paths:
        sys.exit("反映するJSONファイルを指定してください。")
    for name in ("SPREADSHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON"):
        if not os.environ.get(name):
            sys.exit(f"環境変数 {name} を設定してから実行してください(アプリと同じ値)。")
    # 反映ではGeminiを使わないが、common の読み込み時に必須になっているため
    os.environ.setdefault("GEMINI_API_KEY", "unused-by-pachinko-apply")
    import common

    failed = False
    for path in paths:
        with open(path, encoding="utf-8") as f:
            machine = json.load(f)
        existing = next((m for m in common.load_pachinko_machines() if m["name"] == machine.get("name")), None)
        machine["machine_id"] = existing["machine_id"] if existing else ""
        ok, message, _ = common.save_pachinko_machine(machine)
        print(("上書き: " if existing and ok else "") + message + f"  ({path})")
        failed = failed or not ok
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main(sys.argv[1:])
