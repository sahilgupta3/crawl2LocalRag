"""
state.py - SQLite registry (state.db) that makes the crawler incremental. Main thread only.
pages.status: ok (saved) | thin (too little text) | failed | gone (404/410) | redirect | duplicate
"""
import json
import sqlite3
from pathlib import Path

from crawler.utils import now_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    url TEXT PRIMARY KEY, source TEXT NOT NULL, status TEXT NOT NULL, content_hash TEXT, lastmod TEXT,
    local_path TEXT, title TEXT, http_status INTEGER, render_mode TEXT, error TEXT,
    fail_count INTEGER NOT NULL DEFAULT 0, first_seen TEXT NOT NULL, last_checked TEXT NOT NULL, last_changed TEXT);
CREATE INDEX IF NOT EXISTS idx_pages_source ON pages(source);
CREATE TABLE IF NOT EXISTS discovered (
    url TEXT NOT NULL, source TEXT NOT NULL, method TEXT NOT NULL, lastmod TEXT,
    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, PRIMARY KEY (url, method));
CREATE INDEX IF NOT EXISTS idx_disc_source ON discovered(source, method);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, started TEXT NOT NULL, finished TEXT,
    mode TEXT, sites TEXT, summary TEXT);
"""


class StateDB:
    def __init__(self, path):
        self.conn = sqlite3.connect(str(path), timeout=120)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def load_all(self):
        rows = self.conn.execute("SELECT url, source, status, content_hash, lastmod, local_path, last_checked, "
                                 "fail_count FROM pages")
        return {r["url"]: dict(r) for r in rows}

    def record_ok(self, url, source, content_hash, lastmod, local_path, title, http_status, render_mode):
        now = now_iso()
        self.conn.execute("""
            INSERT INTO pages (url, source, status, content_hash, lastmod, local_path, title, http_status,
                               render_mode, error, fail_count, first_seen, last_checked, last_changed)
            VALUES (?, ?, 'ok', ?, ?, ?, ?, ?, ?, NULL, 0, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET source=excluded.source, status='ok', content_hash=excluded.content_hash,
                lastmod=excluded.lastmod, local_path=excluded.local_path, title=excluded.title,
                http_status=excluded.http_status, render_mode=excluded.render_mode, error=NULL, fail_count=0,
                last_checked=excluded.last_checked, last_changed=excluded.last_changed
        """, (url, source, content_hash, lastmod, local_path, title, http_status, render_mode, now, now, now))

    def record_unchanged(self, url, lastmod=None):
        self.conn.execute("UPDATE pages SET lastmod = COALESCE(?, lastmod), last_checked = ?, fail_count = 0, "
                          "error = NULL WHERE url = ?", (lastmod, now_iso(), url))

    def record_thin(self, url, source, lastmod, http_status, render_mode, title):
        now = now_iso()
        self.conn.execute("""
            INSERT INTO pages (url, source, status, lastmod, title, http_status, render_mode, fail_count,
                               first_seen, last_checked)
            VALUES (?, ?, 'thin', ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                status = CASE WHEN pages.status = 'ok' THEN 'ok' ELSE 'thin' END,
                lastmod = CASE WHEN pages.status = 'ok' THEN pages.lastmod ELSE excluded.lastmod END,
                http_status=excluded.http_status, last_checked=excluded.last_checked
        """, (url, source, lastmod, title, http_status, render_mode, now, now))

    def record_failed(self, url, source, lastmod, http_status, render_mode, error, gone=False):
        now = now_iso()
        status = "gone" if gone else "failed"
        self.conn.execute("""
            INSERT INTO pages (url, source, status, lastmod, http_status, render_mode, error, fail_count,
                               first_seen, last_checked)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                status = CASE WHEN excluded.status = 'gone' THEN 'gone'
                              WHEN pages.status = 'ok' THEN 'ok' ELSE 'failed' END,
                lastmod = CASE WHEN excluded.status = 'gone' THEN excluded.lastmod ELSE pages.lastmod END,
                fail_count = pages.fail_count + 1, http_status=excluded.http_status,
                render_mode=excluded.render_mode, error=excluded.error, last_checked=excluded.last_checked
        """, (url, source, status, lastmod if gone else None, http_status, render_mode, error, now, now))

    def record_status(self, url, source, status, lastmod, http_status, render_mode, note):
        """Terminal status (redirect / duplicate); clears any old file reference."""
        now = now_iso()
        self.conn.execute("""
            INSERT INTO pages (url, source, status, lastmod, http_status, render_mode, error, fail_count,
                               first_seen, last_checked)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(url) DO UPDATE SET status=excluded.status, lastmod=excluded.lastmod,
                http_status=excluded.http_status, render_mode=excluded.render_mode, error=excluded.error,
                content_hash=NULL, local_path=NULL, fail_count=0, last_checked=excluded.last_checked
        """, (url, source, status, lastmod, http_status, render_mode, note, now, now))

    def add_discovered(self, source, method, items):
        now = now_iso()
        self.conn.executemany("""
            INSERT INTO discovered (url, source, method, lastmod, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(url, method) DO UPDATE SET source=excluded.source,
                lastmod=COALESCE(excluded.lastmod, discovered.lastmod), last_seen=excluded.last_seen
        """, [(u, source, method, lm, now, now) for u, lm in items.items()])

    def discovered(self, source, method=None):
        q, args = "SELECT url, lastmod FROM discovered WHERE source = ?", [source]
        if method:
            q += " AND method = ?"
            args.append(method)
        return {r["url"]: r["lastmod"] for r in self.conn.execute(q, args)}

    def get_kv(self, key):
        row = self.conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_kv(self, key, value):
        self.conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET "
                          "value = excluded.value", (key, value))

    def start_run(self, mode, sites):
        cur = self.conn.execute("INSERT INTO runs (started, mode, sites) VALUES (?, ?, ?)",
                                (now_iso(), mode, ",".join(sites)))
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id, summary):
        self.conn.execute("UPDATE runs SET finished = ?, summary = ? WHERE id = ?",
                          (now_iso(), json.dumps(summary, default=str), run_id))
        self.conn.commit()

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.commit()
        self.conn.close()
