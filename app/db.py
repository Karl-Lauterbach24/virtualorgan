import sqlite3, os, json, time
from werkzeug.security import generate_password_hash

DB_PATH = "/opt/virtualorgan/data/virtualorgan.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'user',
  created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS midi_routes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  port_name TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'single',
  channel_in INTEGER NOT NULL DEFAULT -1,
  channel_out INTEGER NOT NULL,
  enabled INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS profiles(
  name TEXT PRIMARY KEY,
  data TEXT NOT NULL,
  created REAL NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "require_login": "0",
    "output_device": "alsa:default",
    "soundfont": "/opt/virtualorgan/soundfonts/Organ.sf2",
    "kiosk_enabled": "1",
    "hostname": "virtualorgan",
    "gain": "0.6",
    "reverb": "1",
    "chorus": "0",
    "default_profile": "",
}


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_conn()
    conn.executescript(SCHEMA)
    try:
        conn.execute("ALTER TABLE midi_routes ADD COLUMN mode TEXT NOT NULL DEFAULT 'single'")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()
    cur = conn.execute("SELECT COUNT(*) c FROM users")
    if cur.fetchone()["c"] == 0:
        conn.execute(
            "INSERT INTO users(username,password_hash,role,created) VALUES(?,?,?,?)",
            ("admin", generate_password_hash("virtualorgan"), "admin", time.time()),
        )
    for k, v in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v))
    conn.commit()
    conn.close()


def get_setting(key, default=None):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else default


def set_setting(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    conn.commit()
    conn.close()


def all_settings():
    conn = get_conn()
    rows = conn.execute("SELECT key,value FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


def get_midi_routes():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM midi_routes WHERE enabled=1").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_midi_routes(routes):
    conn = get_conn()
    conn.execute("DELETE FROM midi_routes")
    for r in routes:
        conn.execute(
            "INSERT INTO midi_routes(port_name,mode,channel_in,channel_out,enabled) VALUES(?,?,?,?,1)",
            (r["port_name"], r.get("mode", "single"), int(r.get("channel_in", -1)), int(r["channel_out"])),
        )
    conn.commit()
    conn.close()


def save_profile(name, data: dict):
    conn = get_conn()
    conn.execute(
        "INSERT INTO profiles(name,data,created) VALUES(?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET data=excluded.data, created=excluded.created",
        (name, json.dumps(data), time.time()),
    )
    conn.commit()
    conn.close()


def load_profile(name):
    conn = get_conn()
    row = conn.execute("SELECT data FROM profiles WHERE name=?", (name,)).fetchone()
    conn.close()
    return json.loads(row["data"]) if row else None


def list_profiles():
    conn = get_conn()
    rows = conn.execute("SELECT name FROM profiles ORDER BY name").fetchall()
    conn.close()
    return [r["name"] for r in rows]


def list_users():
    conn = get_conn()
    rows = conn.execute("SELECT id,username,role FROM users ORDER BY username").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_user(username):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(uid):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    return dict(row) if row else None


def add_user(username, password, role="user"):
    conn = get_conn()
    conn.execute(
        "INSERT INTO users(username,password_hash,role,created) VALUES(?,?,?,?)",
        (username, generate_password_hash(password), role, time.time()),
    )
    conn.commit()
    conn.close()


def delete_user(uid):
    conn = get_conn()
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit()
    conn.close()


def set_password(uid, new_password):
    conn = get_conn()
    conn.execute("UPDATE users SET password_hash=? WHERE id=?",
                 (generate_password_hash(new_password), uid))
    conn.commit()
    conn.close()
