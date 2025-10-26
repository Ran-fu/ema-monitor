from flask import Flask, render_template_string

app = Flask(__name__)

@app.route('/')
def home():
    return "✅ Bitunix EMA 吞沒監控系統運作中"

@app.route('/ping')
def ping():
    return "pong"

if __name__ == "__main__":
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
