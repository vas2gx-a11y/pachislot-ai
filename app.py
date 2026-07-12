import os
from flask import Flask, render_template, request, redirect, url_for
import base64
import json
import requests
from datetime import datetime

app = Flask(__name__)

# --- 設定 ---
API_KEY = "AIzaSyDDSLJDVyV2DXZKogMgWhvst_lzRwBiiHk"
MODEL = "gemini-2.5-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent?key={API_KEY}"
DATA_FILE = "database.json" 

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            try: return json.load(f)
            except json.JSONDecodeError: return []
    return []

def save_data(data):
    all_data = load_data()
    all_data.insert(0, data) 
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(all_data, f, ensure_ascii=False, indent=4)

def analyze_image_with_gemini(base64_image):
    prompt = """
    パチスロのデータ画面です。以下のJSON形式でのみ出力してください。
    {"total_games": 0, "big_count": 0, "reg_count": 0, "current_games": 0, "difference_slabs": 0, "graph_features": "", "other_info": ""}
    """
    payload = {"contents": [{"parts": [{"text": prompt}, {"inlineData": {"mimeType": "image/jpeg", "data": base64_image}}]}]}
    headers = {'Content-Type': 'application/json'}
    try:
        response = requests.post(GEMINI_URL, headers=headers, data=json.dumps(payload))
        if response.status_code == 200:
            raw_text = response.json()['candidates'][0]['content']['parts'][0]['text']
            return json.loads(raw_text[raw_text.find('{'):raw_text.rfind('}') + 1])
    except Exception as e:
        print(f"解析エラー: {e}")
    return None

@app.route('/')
def index():
    # 🌟 ここが追加したかった「自動入力」機能です！
    preset_machine = request.args.get('machine_name', '')
    history = load_data()
    return render_template('index.html', history=history, preset_machine=preset_machine)

@app.route('/upload', methods=['POST'])
def upload():
    machine_name = request.form.get('machine_name', '不明な機種').strip()
    user_note = request.form.get('user_note', '').strip()
    file = request.files.get('image')
    
    total, big, reg, current, diff = 0, 0, 0, 0, 0
    graph_features, other_info = "画像なし", "特になし"
    
    if file and file.filename != '':
        image_bytes = file.read()
        base64_image = base64.b64encode(image_bytes).decode('utf-8')
        parsed_data = analyze_image_with_gemini(base64_image)
        if parsed_data:
            total, big, reg = parsed_data.get("total_games", 0), parsed_data.get("big_count", 0), parsed_data.get("reg_count", 0)
            current, diff = parsed_data.get("current_games", 0), parsed_data.get("difference_slabs", 0)
            graph_features, other_info = parsed_data.get("graph_features", "不明"), parsed_data.get("other_info", "特になし")

    # 判定ロジック
    estimation_result = "通常・展開次第"
    if "ToLOVE" in machine_name or "トラブル" in machine_name:
        estimation_result = "高設定濃厚!? (要確認)" if "強示唆" in user_note else "推測中..."

    save_data({
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "machine_name": machine_name,
        "total_games": total, "big_count": big, "reg_count": reg,
        "current_games": current, "difference_slabs": diff,
        "graph_features": graph_features, "other_info": other_info,
        "user_note": user_note, "estimation": estimation_result
    })
    return redirect(url_for('index'))

# app.py の最後をこれに変えてください
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=10000)
    