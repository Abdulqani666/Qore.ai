"""Accounts, chat history and saved files (Postgres). Turned on by the DATABASE_URL variable."""
import base64
import hashlib
import hmac
import os
import re
import time
from contextlib import closing

from werkzeug.security import check_password_hash, generate_password_hash

URL = os.environ.get("DATABASE_URL", "").strip()
ENABLED = bool(URL)
SECRET = (os.environ.get("SECRET_KEY") or hashlib.sha256(("qore:" + URL).encode()).hexdigest()).encode()
EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,200}\.[^@\s]{2,}$")


def conn():
    import psycopg2
    return psycopg2.connect(URL, connect_timeout=10)


def run(sql, args=(), fetch=None):
    """fetch: None | 'one' | 'all'."""
    with closing(conn()) as c:
        with c.cursor() as cur:
            cur.execute(sql, args)
            out = cur.fetchone() if fetch == "one" else cur.fetchall() if fetch == "all" else None
        c.commit()
        return out


def init():
    run("""CREATE TABLE IF NOT EXISTS users(
        id SERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL, pw TEXT NOT NULL, created TIMESTAMPTZ DEFAULT now())""")
    run("""CREATE TABLE IF NOT EXISTS chats(
        id TEXT PRIMARY KEY, uid INT NOT NULL, title TEXT NOT NULL DEFAULT '', items TEXT NOT NULL DEFAULT '[]',
        updated TIMESTAMPTZ DEFAULT now())""")
    run("CREATE INDEX IF NOT EXISTS chats_uid ON chats(uid, updated DESC)")
    run("""CREATE TABLE IF NOT EXISTS files(
        id TEXT PRIMARY KEY, uid INT NOT NULL, name TEXT NOT NULL, data BYTEA NOT NULL, created TIMESTAMPTZ DEFAULT now())""")


def make_token(uid, days=60):
    p = f"{uid}.{int(time.time()) + days * 86400}"
    b = base64.urlsafe_b64encode(p.encode()).decode().rstrip("=")
    return b + "." + hmac.new(SECRET, b.encode(), hashlib.sha256).hexdigest()


def read_token(tok):
    try:
        b, sig = tok.split(".", 1)
        if not hmac.compare_digest(sig, hmac.new(SECRET, b.encode(), hashlib.sha256).hexdigest()):
            return None
        uid, exp = base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode().split(".")
        return int(uid) if int(exp) > time.time() else None
    except Exception:
        return None


def signup(email, password):
    email = (email or "").strip().lower()
    if not EMAIL.match(email):
        return None, "Enter a real email address."
    if len(password or "") < 8:
        return None, "The password needs at least 8 characters."
    if run("SELECT id FROM users WHERE email=%s", (email,), "one"):
        return None, "This email already has an account. Sign in instead."
    row = run("INSERT INTO users(email,pw) VALUES(%s,%s) RETURNING id", (email, generate_password_hash(password)), "one")
    return row[0], None


def login(email, password):
    email = (email or "").strip().lower()
    row = run("SELECT id,pw FROM users WHERE email=%s", (email,), "one")
    if not row or not check_password_hash(row[1], password or ""):
        return None, "Wrong email or password."
    return row[0], None


def email_of(uid):
    r = run("SELECT email FROM users WHERE id=%s", (uid,), "one")
    return r[0] if r else ""
