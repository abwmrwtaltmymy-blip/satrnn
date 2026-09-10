import sqlite3

DB_NAME = "bot_data.db"

def get_connection():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    c = conn.cursor()
    c.execute("CREATE TABLE IF NOT EXISTS groups (chat_id INTEGER PRIMARY KEY, name TEXT, points INTEGER DEFAULT 0, wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE IF NOT EXISTS players (user_id INTEGER PRIMARY KEY, name TEXT, points INTEGER DEFAULT 0, wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE IF NOT EXISTS banned_groups (chat_id INTEGER PRIMARY KEY)")
    c.execute("CREATE TABLE IF NOT EXISTS force_subs (channel_id INTEGER PRIMARY KEY, username TEXT, is_request_mode INTEGER DEFAULT 0)")
    c.execute("CREATE TABLE IF NOT EXISTS all_users (user_id INTEGER PRIMARY KEY, name TEXT)")
    conn.commit()
    conn.close()

def add_points(target_id, target_type, points, name):
    conn = get_connection()
    c = conn.cursor()
    if target_type == "group":
        table, id_col = "groups", "chat_id"
    else:
        table, id_col = "players", "user_id"
    c.execute("INSERT OR IGNORE INTO " + table + " (" + id_col + ", name) VALUES (?, ?)", (target_id, name))
    c.execute("UPDATE " + table + " SET name = ? WHERE " + id_col + " = ?", (name, target_id))
    c.execute("UPDATE " + table + " SET points = points + ? WHERE " + id_col + " = ?", (points, target_id))
    conn.commit()
    conn.close()

def update_win_loss(target_id, target_type, is_win):
    conn = get_connection()
    c = conn.cursor()
    if target_type == "group":
        table, id_col = "groups", "chat_id"
    else:
        table, id_col = "players", "user_id"
    col = "wins" if is_win else "losses"
    c.execute("INSERT OR IGNORE INTO " + table + " (" + id_col + ", name) VALUES (?, ?)", (target_id, "غير معروف"))
    c.execute("UPDATE " + table + " SET " + col + " = " + col + " + 1 WHERE " + id_col + " = ?", (target_id,))
    conn.commit()
    conn.close()

def get_top(target_type, limit=5):
    conn = get_connection()
    c = conn.cursor()
    table = "groups" if target_type == "group" else "players"
    c.execute("SELECT name, points FROM " + table + " ORDER BY points DESC LIMIT ?", (limit,))
    res = c.fetchall()
    conn.close()
    return res

def is_banned(chat_id):
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT 1 FROM banned_groups WHERE chat_id = ?", (chat_id,))
    row = c.fetchone()
    conn.close()
    return row is not None

def ban_group(chat_id):
    conn = get_connection()
    conn.execute("INSERT OR IGNORE INTO banned_groups (chat_id) VALUES (?)", (chat_id,))
    conn.commit()
    conn.close()

def unban_group(chat_id):
    conn = get_connection()
    conn.execute("DELETE FROM banned_groups WHERE chat_id = ?", (chat_id,))
    conn.commit()
    conn.close()

def get_all_groups():
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT chat_id FROM groups")
    res = [r["chat_id"] for r in c.fetchall()]
    conn.close()
    return res

def register_user(user_id, name):
    conn = get_connection()
    conn.execute("INSERT OR REPLACE INTO all_users (user_id, name) VALUES (?, ?)", (user_id, name))
    conn.commit()
    conn.close()

def get_all_users():
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT user_id FROM all_users")
    res = [r["user_id"] for r in c.fetchall()]
    conn.close()
    return res

def add_force_sub(channel_id, username, is_request_mode=0):
    conn = get_connection()
    conn.execute("INSERT OR REPLACE INTO force_subs (channel_id, username, is_request_mode) VALUES (?, ?, ?)", (channel_id, username, is_request_mode))
    conn.commit()
    conn.close()

def remove_force_sub(channel_id):
    conn = get_connection()
    conn.execute("DELETE FROM force_subs WHERE channel_id = ?", (channel_id,))
    conn.commit()
    conn.close()

def get_force_subs():
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT * FROM force_subs")
    res = c.fetchall()
    conn.close()
    return res

def get_stats():
    conn = get_connection()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) as c FROM groups")
    g = c.fetchone()["c"]
    c.execute("SELECT COUNT(*) as c FROM players")
    p = c.fetchone()["c"]
    c.execute("SELECT COUNT(*) as c FROM banned_groups")
    b = c.fetchone()["c"]
    c.execute("SELECT COUNT(*) as c FROM force_subs")
    s = c.fetchone()["c"]
    c.execute("SELECT COUNT(*) as c FROM all_users")
    u = c.fetchone()["c"]
    conn.close()
    return {"groups": g, "players": p, "banned": b, "subs": s, "users": u}