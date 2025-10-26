import os
from flask import Flask

app = Flask(__name__)

@app.route("/")
def home():
    return "✅ Bitunix EMA 監控 Web Service 運作中"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    print(f"啟動 Web Service，port={port}")
    app.run(host="0.0.0.0", port=port)
