import gspread
from oauth2client.service_account import ServiceAccountCredentials

def save_to_spreadsheet(data):
    # 認証スコープ
    scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
    
    # ダウンロードしたJSONキーのパス（ファイル名を合わせる）
    creds = ServiceAccountCredentials.from_json_keyfile_name('credentials.json', scope)
    client = gspread.authorize(creds)
    
    # スプレッドシートを開く
    sheet = client.open('パチスロ稼働記録').sheet1
    
    # データを1行にまとめる
    row = [
        data['date'], 
        data['machine_name'], 
        data['total_games'], 
        data['big_count'], 
        data['reg_count'], 
        data['current_games'], 
        data['difference_slabs'], 
        data['user_note'], 
        data['estimation']
    ]
    
    # シートに追加
    sheet.append_row(row)