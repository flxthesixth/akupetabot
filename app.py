#!/usr/bin/env python3
import json
import os
import hashlib
import re
import secrets
import shutil
import sqlite3
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "tasks.sqlite3"
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
API = f"https://api.telegram.org/bot{TOKEN}/"
PENDING = {}
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(",", " ").split() if x.isdigit()}
TOKEN_SALT = os.environ.get("ACCESS_TOKEN_SALT", "")
HELP = """Aku Peta — tracker tugas pribadi.

/add <tugas> — tambah tugas
/list — lihat semua tugas
/done <nomor> — tandai selesai
/priority <nomor> — aktif/nonaktifkan prioritas 🔥
/delete <nomor> — hapus satu tugas
/clear — hapus semua tugas
/redeem <token> — aktifkan akses
/status — cek masa aktif
/help — bantuan

Kirim teks biasa juga akan ditambahkan sebagai tugas."""


def db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL,
        text TEXT NOT NULL CHECK(length(text) BETWEEN 1 AND 500),
        done INTEGER NOT NULL DEFAULT 0 CHECK(done IN (0,1)),
        created_at INTEGER NOT NULL,
        completed_at INTEGER,
        priority INTEGER NOT NULL DEFAULT 0 CHECK(priority IN (0,1)),
        due_at INTEGER,
        reminded INTEGER NOT NULL DEFAULT 0 CHECK(reminded IN (0,1)),
        due_notified INTEGER NOT NULL DEFAULT 0 CHECK(due_notified IN (0,1))
    )""")
    columns = {row[1] for row in con.execute("PRAGMA table_info(tasks)")}
    for name, definition in {
        "priority": "INTEGER NOT NULL DEFAULT 0",
        "due_at": "INTEGER",
        "reminded": "INTEGER NOT NULL DEFAULT 0",
        "due_notified": "INTEGER NOT NULL DEFAULT 0",
    }.items():
        if name not in columns:
            con.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
    con.execute("CREATE INDEX IF NOT EXISTS tasks_owner_done ON tasks(owner_id, done, id)")
    con.execute("CREATE TABLE IF NOT EXISTS users(owner_id INTEGER PRIMARY KEY, username TEXT, access_until INTEGER NOT NULL)")
    user_columns = {row[1] for row in con.execute("PRAGMA table_info(users)")}
    if "username" not in user_columns:
        con.execute("ALTER TABLE users ADD COLUMN username TEXT")
    con.execute("""CREATE TABLE IF NOT EXISTS access_tokens(
        token_hash TEXT PRIMARY KEY,
        days INTEGER NOT NULL CHECK(days BETWEEN 1 AND 366),
        used_by INTEGER,
        used_at INTEGER,
        created_at INTEGER NOT NULL
    )""")
    con.commit()
    return con


def token_hash(code):
    return hashlib.sha256((TOKEN_SALT + code).encode()).hexdigest()


def generate_access_token(days):
    if days not in (7, 30):
        raise ValueError("Durasi token hanya 7 atau 30 hari.")
    code = secrets.token_urlsafe(24)
    with db() as con:
        con.execute("INSERT INTO access_tokens(token_hash,days,created_at) VALUES(?,?,?)", (token_hash(code), days, int(time.time())))
    return code


def redeem_access_token(owner_id, code):
    code = code.strip()
    if not 20 <= len(code) <= 100:
        return None
    now = int(time.time())
    with db() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT days,used_by FROM access_tokens WHERE token_hash=?", (token_hash(code),)).fetchone()
        if not row or row["used_by"] is not None:
            return None
        current = con.execute("SELECT access_until FROM users WHERE owner_id=?", (owner_id,)).fetchone()
        until = max(now, current["access_until"] if current else now) + row["days"] * 86400
        con.execute("INSERT INTO users(owner_id,access_until) VALUES(?,?) ON CONFLICT(owner_id) DO UPDATE SET access_until=excluded.access_until", (owner_id, until))
        con.execute("UPDATE access_tokens SET used_by=?,used_at=? WHERE token_hash=? AND used_by IS NULL", (owner_id, now, token_hash(code)))
    return until


def ensure_user(owner_id, username=None):
    with db() as con:
        con.execute("INSERT OR IGNORE INTO users(owner_id,username,access_until) VALUES(?,?,0)", (owner_id, username))
        if username:
            con.execute("UPDATE users SET username=? WHERE owner_id=?", (username, owner_id))


def access_until(owner_id):
    with db() as con:
        row = con.execute("SELECT access_until FROM users WHERE owner_id=?", (owner_id,)).fetchone()
    return row["access_until"] if row else 0


def user_access_list():
    now = int(time.time())
    with db() as con:
        rows = con.execute("SELECT owner_id,username,access_until FROM users ORDER BY owner_id").fetchall()
    lines = []
    for row in rows:
        if row["access_until"] <= now:
            status = "tidak aktif"
        else:
            expiry = datetime.fromtimestamp(row["access_until"], ZoneInfo("Asia/Makassar"))
            status = f"aktif sampai {expiry:%d-%m-%Y %H:%M} WITA"
        username = "@" + row["username"] if row["username"] else "-"
        lines.append(f"{row['owner_id']} | {username} | {status}")
    return "Daftar user:\n" + "\n".join(lines) if lines else "Belum ada user."


def deactivate_user(identifier):
    value = identifier.strip().lstrip("@")
    if not value:
        return False
    with db() as con:
        if value.isdigit():
            cur = con.execute("UPDATE users SET access_until=0 WHERE owner_id=?", (int(value),))
        else:
            cur = con.execute("UPDATE users SET access_until=0 WHERE lower(username)=lower(?)", (value,))
        return cur.rowcount > 0


def tg(method, data=None):
    body = urllib.parse.urlencode(data or {}).encode()
    with urllib.request.urlopen(API + method, body, timeout=40) as response:
        return json.load(response)


def send(chat_id, text):
    tg("sendMessage", {"chat_id": chat_id, "text": text})


def tasks(owner_id):
    with db() as con:
        return con.execute("""SELECT id,text,done,priority,due_at FROM tasks WHERE owner_id=?
            ORDER BY done, CASE WHEN priority=1 THEN 0 WHEN due_at IS NOT NULL THEN 1 ELSE 2 END, id""", (owner_id,)).fetchall()


def numbered(owner_id):
    rows = tasks(owner_id)
    if not rows:
        return "Belum ada tugas."
    lines = []
    for i, row in enumerate(rows, 1):
        if row["done"]:
            rendered = f"{row['text']} ✅"
        elif row["due_at"]:
            due = datetime.fromtimestamp(row["due_at"], ZoneInfo("Asia/Makassar"))
            rendered = f"{row['text']} | {due:%d-%m-%Y} | {due:%H:%M} | 🔔"
        else:
            rendered = row["text"] + (" 🔥" if row["priority"] else "")
        lines.append(f"{i}. {rendered}")
    return "Daftar tugas:\n" + "\n".join(lines)


def task_id_at(owner_id, position):
    rows = tasks(owner_id)
    return rows[position - 1]["id"] if 1 <= position <= len(rows) else None


def add_task(owner_id, text, due_at=None):
    text = " ".join(text.split())
    if not text or len(text) > 500:
        raise ValueError("Tugas harus 1–500 karakter.")
    with db() as con:
        con.execute("INSERT INTO tasks(owner_id,text,created_at,due_at) VALUES(?,?,?,?)", (owner_id, text, int(time.time()), due_at))


def wants_reminder(text):
    return bool(re.search(r"\b(reminder|ingatkan|pengingat|alarm|alert|ingat)\b", text, re.I))


def parse_reminder(text, now=None):
    match = re.fullmatch(r"(?:reminder|ingatkan|pengingat|alarm|alert|ingat)(?: aku)? (.+?) (?:(besok|hari ini|nanti) )?jam (\d{1,2})(?::(\d{2}))? ?(pagi|siang|sore|malam)?", text, re.I)
    if not match:
        return None
    task, day, hour, minute, period = match.groups()
    hour, minute = int(hour), int(minute or 0)
    if period in ("siang", "sore", "malam") and hour < 12:
        hour += 12
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("Jam tidak valid.")
    tz = ZoneInfo("Asia/Makassar")
    local = now or datetime.now(tz)
    day = (day or "hari ini").lower()
    due = (local + timedelta(days=day == "besok")).replace(hour=hour, minute=minute, second=0, microsecond=0)
    if due <= local:
        raise ValueError("Waktu reminder sudah lewat.")
    return task, int(due.timestamp()), due


def parse_position(text):
    try:
        value = int(text.strip())
        if value < 1:
            raise ValueError
        return value
    except ValueError:
        return None


def apply_numbered_action(owner_id, position, action):
    task_id = task_id_at(owner_id, position)
    if not task_id:
        return "Nomor tugas tidak valid. Gunakan /list."
    with db() as con:
        if action == "done":
            con.execute("UPDATE tasks SET done=1,completed_at=? WHERE id=? AND owner_id=?", (int(time.time()), task_id, owner_id))
            result = "Tugas selesai."
        elif action == "priority":
            con.execute("UPDATE tasks SET priority=1-priority WHERE id=? AND owner_id=?", (task_id, owner_id))
            result = "Prioritas diperbarui."
        else:
            con.execute("DELETE FROM tasks WHERE id=? AND owner_id=?", (task_id, owner_id))
            result = "Tugas dihapus."
    return result + "\n\n" + numbered(owner_id)


def handle(message):
    text = (message.get("text") or "").strip()
    if not text:
        return
    chat_id = message["chat"]["id"]
    owner_id = message["from"]["id"]
    ensure_user(owner_id, message.get("from", {}).get("username"))
    command, _, arg = text.partition(" ")
    command = command.split("@", 1)[0].lower()
    try:
        public = command in ("/start", "/redeem", "/status", "/admin", "/gentoken", "/users", "/deactivate")
        if owner_id in PENDING and PENDING[owner_id] == "redeem" and not text.startswith("/"):
            until = redeem_access_token(owner_id, text)
            PENDING.pop(owner_id, None)
            if not until:
                send(chat_id, "Token salah atau sudah digunakan.")
            else:
                expiry = datetime.fromtimestamp(until, ZoneInfo("Asia/Makassar"))
                send(chat_id, f"Token valid. Akses aktif sampai {expiry:%d-%m-%Y %H:%M} WITA.")
            return
        if not public and access_until(owner_id) <= int(time.time()):
            PENDING.pop(owner_id, None)
            send(chat_id, "Akses habis atau belum aktif. Ketik /redeem lalu kirim token.")
            return
        if owner_id in PENDING and not text.startswith("/"):
            action = PENDING[owner_id]
            if action == "clear":
                answer = text.lower()
                if answer not in ("y", "n"):
                    send(chat_id, "Jawab y atau n.")
                    return
                PENDING.pop(owner_id)
                if answer == "n":
                    send(chat_id, "Dibatalkan.")
                    return
                with db() as con:
                    count = con.execute("DELETE FROM tasks WHERE owner_id=?", (owner_id,)).rowcount
                send(chat_id, f"{count} tugas dihapus.")
                return
            position = parse_position(text)
            if not position:
                send(chat_id, "Kirim nomor urut tugas, atau /cancel.")
                return
            PENDING.pop(owner_id)
            send(chat_id, apply_numbered_action(owner_id, position, action))
            return
        if command == "/start":
            if access_until(owner_id) > int(time.time()):
                send(chat_id, HELP)
            else:
                send(chat_id, "Bot memerlukan token akses.\nKetik /redeem lalu kirim token.")
            return
        if command == "/redeem":
            if not arg.strip():
                PENDING[owner_id] = "redeem"
                send(chat_id, "Kirim token akses.")
                return
            until = redeem_access_token(owner_id, arg)
            if not until:
                send(chat_id, "Token salah atau sudah digunakan.")
            else:
                expiry = datetime.fromtimestamp(until, ZoneInfo("Asia/Makassar"))
                send(chat_id, f"Token valid. Akses aktif sampai {expiry:%d-%m-%Y %H:%M} WITA.")
            return
        if command == "/admin":
            if owner_id not in ADMIN_IDS:
                send(chat_id, "Perintah tidak tersedia.")
            else:
                send(chat_id, "Admin aktif.\n/gentoken 7 — token trial\n/gentoken 30 — token subscription\n/users — daftar user dan status akses\n/deactivate @username — nonaktifkan user")
            return
        if command == "/gentoken":
            if owner_id not in ADMIN_IDS:
                send(chat_id, "Perintah tidak tersedia.")
                return
            try:
                days = int(arg)
            except ValueError:
                raise ValueError("Gunakan /gentoken 7 atau /gentoken 30.")
            send(chat_id, f"Token {days} hari:\n{generate_access_token(days)}")
            return
        if command == "/users":
            if owner_id not in ADMIN_IDS:
                send(chat_id, "Perintah tidak tersedia.")
            else:
                send(chat_id, user_access_list())
            return
        if command == "/deactivate":
            if owner_id not in ADMIN_IDS:
                send(chat_id, "Perintah tidak tersedia.")
            elif deactivate_user(arg):
                send(chat_id, "User dinonaktifkan.")
            else:
                send(chat_id, "User tidak ditemukan. Pakai /users lalu /deactivate @username atau /deactivate ID.")
            return
        if command == "/status":
            until = access_until(owner_id)
            if until <= int(time.time()):
                send(chat_id, "Akses tidak aktif. Gunakan /redeem TOKEN.")
            else:
                expiry = datetime.fromtimestamp(until, ZoneInfo("Asia/Makassar"))
                send(chat_id, f"Akses aktif sampai {expiry:%d-%m-%Y %H:%M} WITA.")
            return
        if command == "/help":
            send(chat_id, HELP)
        elif command == "/list":
            send(chat_id, numbered(owner_id))
        elif command == "/add":
            add_task(owner_id, arg)
            send(chat_id, "Tugas ditambahkan.\n\n" + numbered(owner_id))
        elif command in ("/done", "/priority", "/delete"):
            position = parse_position(arg)
            action = command.removeprefix("/")
            if not position:
                if not tasks(owner_id):
                    send(chat_id, "Belum ada tugas.")
                    return
                PENDING[owner_id] = action
                verb = {"done": "diselesaikan", "priority": "diatur prioritasnya", "delete": "dihapus"}[action]
                send(chat_id, numbered(owner_id) + f"\n\nKirim nomor urut tugas yang mau {verb}.")
                return
            send(chat_id, apply_numbered_action(owner_id, position, action))
        elif command == "/cancel":
            PENDING.pop(owner_id, None)
            send(chat_id, "Dibatalkan.")
        elif command == "/clear":
            if not tasks(owner_id):
                send(chat_id, "Belum ada tugas.")
                return
            PENDING[owner_id] = "clear"
            send(chat_id, "Hapus semua task? Jawab y/n.")
        elif text.startswith("/"):
            send(chat_id, "Perintah tidak dikenal. Gunakan /help.")
        else:
            reminder = parse_reminder(text)
            if reminder:
                task, due_at, due = reminder
                add_task(owner_id, task, due_at)
                send(chat_id, f"Reminder dibuat: {task}\nWaktu: {due:%d-%m-%Y %H:%M} WITA\nNotifikasi: 15 menit sebelumnya dan tepat waktu.")
            elif wants_reminder(text):
                send(chat_id, "Format reminder belum terbaca. Contoh:\ningatkan aku pergi besok jam 9 pagi")
            else:
                add_task(owner_id, text)
                send(chat_id, "Tugas ditambahkan.\n\n" + numbered(owner_id))
    except ValueError as exc:
        send(chat_id, str(exc))
    except Exception as exc:
        print(f"handle error: {exc!r}", flush=True)
        send(chat_id, "Terjadi error. Coba lagi.")


def send_due_reminders():
    now = int(time.time())
    with db() as con:
        rows = con.execute("""SELECT t.id,t.owner_id,t.text,t.due_at,t.reminded,t.due_notified
            FROM tasks t JOIN users u ON u.owner_id=t.owner_id
            WHERE t.done=0 AND t.due_at IS NOT NULL AND u.access_until>?
            AND (t.reminded=0 OR t.due_notified=0)""", (now,)).fetchall()
        for row in rows:
            if not row["reminded"] and now >= row["due_at"] - 900:
                send(row["owner_id"], f"Reminder 15 menit lagi:\n{row['text']}")
                con.execute("UPDATE tasks SET reminded=1 WHERE id=?", (row["id"],))
            if not row["due_notified"] and now >= row["due_at"]:
                send(row["owner_id"], f"Waktunya:\n{row['text']}")
                con.execute("UPDATE tasks SET due_notified=1 WHERE id=?", (row["id"],))


def self_check():
    global DB_PATH
    original = DB_PATH
    scratch = Path(tempfile.mkdtemp(prefix="akupetabot-self-check-"))
    DB_PATH = scratch / "tasks.sqlite3"
    try:
        add_task(1, "Task A")
        add_task(2, "Task B")
        assert [r["text"] for r in tasks(1)] == ["Task A"]
        assert [r["text"] for r in tasks(2)] == ["Task B"]
        assert task_id_at(1, 2) is None
        assert apply_numbered_action(1, 1, "priority").startswith("Prioritas")
        assert tasks(1)[0]["priority"] == 1
        assert apply_numbered_action(1, 1, "done").startswith("Tugas selesai.")
        assert tasks(1)[0]["done"] == 1 and "Task A ✅" in numbered(1) and "🔥" not in numbered(1)
        assert [r["text"] for r in tasks(2)] == ["Task B"]
        parsed = parse_reminder("ingatkan aku pergi ke rumah sakit besok jam 9 pagi", datetime(2026, 9, 8, 8, tzinfo=ZoneInfo("Asia/Makassar")))
        assert parsed and parsed[0] == "pergi ke rumah sakit" and parsed[2].hour == 9
        later = parse_reminder("ingatkan aku mau pergi nanti jam 10 malam", datetime(2026, 9, 8, 8, tzinfo=ZoneInfo("Asia/Makassar")))
        assert later and later[0] == "mau pergi" and later[2].hour == 22
        for keyword in ("reminder", "ingatkan", "pengingat", "alarm", "alert", "ingat"):
            assert parse_reminder(f"{keyword} aku tes nanti jam 10 malam", datetime(2026, 9, 8, 8, tzinfo=ZoneInfo("Asia/Makassar")))
        assert wants_reminder("tolong buat alarm untuk nanti")
        code = generate_access_token(7)
        until = redeem_access_token(10, code)
        assert until and until > int(time.time()) + 6 * 86400
        assert redeem_access_token(11, code) is None
        extension = generate_access_token(30)
        assert redeem_access_token(10, extension) > until + 29 * 86400
        assert access_until(11) == 0
        ensure_user(12, "alice")
        assert "12 | @alice | tidak aktif" in user_access_list()
        code3 = generate_access_token(7)
        assert redeem_access_token(12, code3)
        assert deactivate_user("@alice") and access_until(12) == 0
        sent = []
        original_send = globals()["send"]
        globals()["send"] = lambda chat_id, text: sent.append(text)
        try:
            code2 = generate_access_token(7)
            handle({"text": "/redeem", "chat": {"id": 13}, "from": {"id": 13}})
            handle({"text": code2, "chat": {"id": 13}, "from": {"id": 13}})
            assert sent[-2] == "Kirim token akses."
            assert sent[-1].startswith("Token valid. Akses aktif sampai ")
        finally:
            globals()["send"] = original_send
            PENDING.pop(13, None)
        add_task(3, "Biasa")
        add_task(3, "Jadwal", parsed[1])
        add_task(3, "Penting")
        apply_numbered_action(3, 3, "priority")
        assert [row["text"] for row in tasks(3)] == ["Penting", "Jadwal", "Biasa"]
        assert "Penting 🔥" in numbered(3) and "Jadwal | 09-09-2026 | 09:00 | 🔔" in numbered(3)
        apply_numbered_action(3, 2, "done")
        assert "Jadwal ✅" in numbered(3) and "Jadwal |" not in numbered(3)
        print("self-check OK")
    finally:
        shutil.rmtree(scratch)
        DB_PATH = original


def main():
    if not TOKEN or not TOKEN_SALT or not ADMIN_IDS:
        raise RuntimeError("TELEGRAM_BOT_TOKEN, ACCESS_TOKEN_SALT, dan ADMIN_IDS wajib diatur.")
    db().close()
    tg("deleteWebhook", {"drop_pending_updates": "false"})
    offset = 0
    print("Aku Peta bot started", flush=True)
    while True:
        try:
            send_due_reminders()
            updates = tg("getUpdates", {"timeout": 30, "offset": offset}).get("result", [])
            for update in updates:
                offset = max(offset, update["update_id"] + 1)
                if message := update.get("message"):
                    handle(message)
        except Exception as exc:
            print(f"poll error: {exc!r}", flush=True)
            time.sleep(3)


if __name__ == "__main__":
    if "--self-check" in __import__("sys").argv:
        self_check()
    else:
        main()
