import requests

# ===== 輸入你的 Bot Token =====
BOT_TOKEN = "8207214560:AAE6BbWOMUry65_NxiNEnfQnflp-lYPMlMI"

def get_chat_id():
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        resp = requests.get(url, timeout=10).json()
        if not resp.get("ok"):
            print(f"❌ 錯誤: {resp}")
            return
        results = resp.get("result", [])
        if not results:
            print("⚠️ 尚未收到任何訊息，請先對 Bot 發送 /start 或在群組發一條訊息")
            return
        print("📌 找到以下 Chat ID：")
        for item in results:
            msg = item.get("message")
            if not msg:
                continue
            chat = msg.get("chat")
            chat_id = chat.get("id")
            chat_type = chat.get("type")
            chat_name = chat.get("first_name") if chat_type == "private" else chat.get("title")
            print(f"{chat_type}: {chat_name} → {chat_id}")
    except Exception as e:
        print(f"❌ 發生異常: {e}")

if __name__ == "__main__":
    get_chat_id()
