"""SQLite storage. The .db file lives in the repo and GitHub Actions commits it after each run."""
import hashlib
import json
import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "fragprice.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS posts (
    source       TEXT NOT NULL,          -- reddit | facebook | mercari
    post_id      TEXT NOT NULL,
    url          TEXT,
    title        TEXT,
    text         TEXT,
    created_utc  INTEGER,
    text_hash    TEXT,                   -- hash of text at last Claude check (skip re-check if unchanged)
    checks       INTEGER DEFAULT 0,      -- how many times Claude has read this post
    last_check   INTEGER,
    error        TEXT,
    PRIMARY KEY (source, post_id)
);

CREATE TABLE IF NOT EXISTS sales (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    post_id      TEXT NOT NULL,
    url          TEXT,
    date_utc     INTEGER,                -- post date (sold date for Mercari)
    brand        TEXT,
    name         TEXT,
    key          TEXT,                   -- canonical "brand|name" slug used for grouping
    concentration TEXT,
    size_ml      REAL,
    fill_pct     REAL,
    condition    TEXT,
    box          TEXT,                   -- yes | no | unknown
    cap          TEXT,                   -- yes | no | unknown
    presentation TEXT,
    qty          INTEGER DEFAULT 1,      -- bottles this row represents (e.g. "5 sold" at one price)                   -- seller's own words, e.g. "full presentation", "no cap"
    price        REAL,
    currency     TEXT,
    ships_incl   INTEGER,
    status       TEXT,                   -- sold | available | pending | unknown
    bundle       INTEGER DEFAULT 0,
    confidence   REAL,
    evidence     TEXT
);
CREATE INDEX IF NOT EXISTS idx_sales_key  ON sales(key);
CREATE INDEX IF NOT EXISTS idx_sales_post ON sales(source, post_id);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect(path=DB_PATH):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    have = {r["name"] for r in con.execute("PRAGMA table_info(sales)")}
    for col, typ in (("box", "TEXT"), ("cap", "TEXT"), ("presentation", "TEXT"), ("qty", "INTEGER DEFAULT 1")):
        if col not in have:  # upgrade databases created before these columns existed
            con.execute(f"ALTER TABLE sales ADD COLUMN {col} {typ}")
    return con


def text_hash(s: str) -> str:
    return hashlib.sha1((s or "").encode()).hexdigest()


def get_meta(con, k, default=None):
    r = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return json.loads(r["v"]) if r else default


def set_meta(con, k, v):
    con.execute("INSERT OR REPLACE INTO meta(k, v) VALUES (?, ?)", (k, json.dumps(v)))
    con.commit()


def upsert_post(con, source, post_id, url, title, text, created_utc):
    """Insert a post, or refresh its text if we've seen it before (sellers edit in 'SOLD')."""
    con.execute(
        """INSERT INTO posts(source, post_id, url, title, text, created_utc)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(source, post_id) DO UPDATE SET
             title=excluded.title,
             text=CASE WHEN excluded.text IS NOT NULL AND excluded.text != '' THEN excluded.text ELSE posts.text END,
             url=COALESCE(excluded.url, posts.url)""",
        (source, str(post_id), url, title, text, int(created_utc or time.time())),
    )


def due_posts(con, source, check_ages_days, limit=500):
    """Posts whose next scheduled check age has been reached.

    check_ages_days e.g. [4, 21]: first read at 4 days old (time for SOLD edits),
    re-read at 21 days old. [0] = read once immediately (Mercari, already sold).
    """
    now = time.time()
    out = []
    for i, age in enumerate(check_ages_days):
        cutoff = now - age * 86400
        rows = con.execute(
            "SELECT * FROM posts WHERE source=? AND checks=? AND created_utc<=? "
            "ORDER BY created_utc DESC LIMIT ?",  # newest first: recent prices matter most
            (source, i, cutoff, limit),
        ).fetchall()
        out.extend(rows)
    return out[:limit]


def replace_sales(con, source, post_id, url, date_utc, items):
    con.execute("DELETE FROM sales WHERE source=? AND post_id=?", (source, str(post_id)))
    for it in items:
        con.execute(
            """INSERT INTO sales(source, post_id, url, date_utc, brand, name, key, concentration,
               size_ml, fill_pct, condition, box, cap, presentation, qty, price, currency, ships_incl, status, bundle,
               confidence, evidence) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                source, str(post_id), url, int(date_utc or 0),
                it.get("brand"), it.get("name"), it.get("key"), it.get("concentration"),
                it.get("size_ml"), it.get("fill_pct"), it.get("condition"),
                it.get("box"), it.get("cap"), it.get("presentation"), int(it.get("qty") or 1),
                it.get("price"), it.get("currency") or "USD",
                None if it.get("price_includes_shipping") is None else int(bool(it["price_includes_shipping"])),
                it.get("status"), int(bool(it.get("bundle"))),
                it.get("confidence"), it.get("sold_evidence"),
            ),
        )


def mark_checked(con, source, post_id, thash, error=None):
    con.execute(
        "UPDATE posts SET checks=checks+1, last_check=?, text_hash=?, error=? WHERE source=? AND post_id=?",
        (int(time.time()), thash, error, source, str(post_id)),
    )
