import os
import sqlite3
from flask import Flask, jsonify

DB_PATH = os.environ.get("DB_PATH", "/data/app.db")

app = Flask(__name__)


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS visits ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "ts TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    return conn


@app.route("/")
def index():
    conn = get_db()
    conn.execute("INSERT INTO visits DEFAULT VALUES")
    conn.commit()
    count = conn.execute("SELECT COUNT(*) FROM visits").fetchone()[0]
    conn.close()
    return jsonify(status="ok", env=os.environ.get("ENV", "dev"), visits=count)


@app.route("/health")
def health():
    return jsonify(status="healthy")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
