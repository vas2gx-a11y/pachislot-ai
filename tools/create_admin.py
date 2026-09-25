"""
管理者アカウントを作る(すでにあればパスワードを設定し直す)。

    SPREADSHEET_ID=... GOOGLE_SERVICE_ACCOUNT_JSON="$(cat key.json)" python3 tools/create_admin.py

画面から作れるのはメンバーだけにしているので、最初の管理者はここで作る
(ログイン画面より前に誰でも管理者を作れる入口を、本番に置かないため)。
スプレッドシートに直接書くので、手元のPCから実行すれば Render 側の操作は要らない。
管理者のパスワードを忘れたときも、これを実行し直せば再設定できる。
"""
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main():
    for name in ("SPREADSHEET_ID", "GOOGLE_SERVICE_ACCOUNT_JSON"):
        if not os.environ.get(name):
            sys.exit(f"環境変数 {name} を設定してから実行してください(アプリと同じ値)。")
    # 管理者の作成ではGeminiを使わないが、common の読み込み時に必須になっているため
    os.environ.setdefault("GEMINI_API_KEY", "unused-by-create-admin")
    import common

    owner = common.find_user(common.OWNER_USER_ID)
    if owner:
        print(f"管理者はすでにあります(ログインID: {owner['login_id']})。パスワードを設定し直します。")
    else:
        login_id = input("管理者のログインID(半角英数字と _ . - の3〜32文字): ").strip()
        display_name = input("画面に出す名前(空ならログインIDと同じ): ").strip()

    password = getpass.getpass("パスワード(8文字以上): ")
    if password != getpass.getpass("パスワード(確認): "):
        sys.exit("確認用のパスワードが一致しません。")

    if owner:
        ok, message = common.set_user_password(owner["user_id"], password)
    else:
        ok, message = common.create_user(login_id, display_name, password,
                                         role=common.ROLE_ADMIN, user_id=common.OWNER_USER_ID)
    print(message)
    if not ok:
        sys.exit(1)


if __name__ == "__main__":
    main()
