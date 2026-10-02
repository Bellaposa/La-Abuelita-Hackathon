from flask import Flask, jsonify, send_from_directory
import os
from bazaar_sdk import Bazaar

app = Flask(__name__)

# Assicuriamoci di non crashare se la chiave manca all'avvio, ma gestiamola
url = os.environ.get("BAZAAR_URL", "https://bazaar.causaprima.ai")
key = os.environ.get("BAZAAR_KEY", "NOT_SET")
b = Bazaar(url, key) if key != "NOT_SET" else None

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/state')
def state():
    if not b:
        return jsonify({"error": "BAZAAR_KEY non impostata nel terminale!"}), 500
    try:
        me = b.me()
        clock = b.clock()
        return jsonify({"me": me, "clock": clock})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == '__main__':
    print("🚀 Avvio Web Dashboard su http://localhost:5050")
    app.run(port=5050, debug=False)
