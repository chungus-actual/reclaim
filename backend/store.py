"""SQLite for what the page writes: shortlist, keep pins, deletion log, capacity history.

The deletion log is the "record of having had it": every row keeps the title,
size, files and who watched / requested it at the moment it was deleted.
"""
import json
import sqlite3
import time

import config as C

DB = C.DATA_DIR / "reclaim.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS shortlist (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,            -- movie | show | season | version
  key TEXT NOT NULL,             -- rating key of the thing deleted (season key for seasons)
  title_key TEXT NOT NULL,       -- owning movie/show
  media_id INTEGER NOT NULL DEFAULT 0,  -- versions only (0 otherwise: NULLs never collide in UNIQUE)
  label TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  added_at REAL NOT NULL,
  UNIQUE(kind, key, media_id)
);
CREATE TABLE IF NOT EXISTS keep (key TEXT PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS deletions (
  id INTEGER PRIMARY KEY,
  at REAL NOT NULL,
  kind TEXT NOT NULL,
  key TEXT NOT NULL,
  title_key TEXT NOT NULL,
  label TEXT NOT NULL,
  bytes INTEGER NOT NULL,
  ok INTEGER NOT NULL,
  plex TEXT,
  arr TEXT,
  meta TEXT                      -- json: files, viewers, plays, requested by, ids
);
CREATE TABLE IF NOT EXISTS downgrades (
  id INTEGER PRIMARY KEY,
  created REAL NOT NULL, updated REAL NOT NULL,
  app TEXT NOT NULL, item_id INTEGER NOT NULL,   -- radarr movie id / sonarr series id
  title_key TEXT NOT NULL, season INTEGER,       -- season number for TV
  label TEXT NOT NULL, target INTEGER NOT NULL,
  release TEXT, quality TEXT, indexer TEXT, new_size INTEGER,
  old_size INTEGER, old_quality TEXT, old_profile INTEGER,
  old_file_id INTEGER, old_file_ids TEXT,
  state TEXT NOT NULL,                           -- grabbed | imported | failed | cancelled
  note TEXT, final_size INTEGER, folder TEXT, section TEXT,
  old_path TEXT                                  -- replacements: the file the import swaps out, in Plex's form
);
CREATE TABLE IF NOT EXISTS capacity (
  at REAL PRIMARY KEY,
  total INTEGER, used INTEGER, free INTEGER, indexed INTEGER, reason TEXT
);
"""


def conn():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        cols = {r[1] for r in c.execute("PRAGMA table_info(downgrades)")}
        if "old_path" not in cols:      # databases from before replacements
            c.execute("ALTER TABLE downgrades ADD COLUMN old_path TEXT")


def rows(sql, *args):
    with conn() as c:
        return [dict(r) for r in c.execute(sql, args)]


def shortlist():
    return rows("SELECT * FROM shortlist ORDER BY bytes DESC")


def shortlist_add(items):
    """A whole title absorbs its listed seasons/versions; parts of a listed title are skipped."""
    now = time.time()
    with conn() as c:
        for it in items:
            if it["kind"] in ("movie", "show"):
                c.execute("DELETE FROM shortlist WHERE title_key=? AND kind IN ('season','version')", (it["title_key"],))
            elif c.execute("SELECT 1 FROM shortlist WHERE title_key=? AND kind IN ('movie','show')",
                           (it["title_key"],)).fetchone():
                continue
            c.execute("INSERT OR IGNORE INTO shortlist(kind,key,title_key,media_id,label,bytes,added_at) VALUES (?,?,?,?,?,?,?)",
                      (it["kind"], it["key"], it["title_key"], it.get("media_id") or 0, it["label"], int(it["bytes"]), now))


def shortlist_remove(ids):
    with conn() as c:
        c.executemany("DELETE FROM shortlist WHERE id=?", [(i,) for i in ids])


def shortlist_clear():
    with conn() as c:
        c.execute("DELETE FROM shortlist")


def keep_keys():
    return {r["key"] for r in rows("SELECT key FROM keep")}


def keep_set(key, on):
    with conn() as c:
        if on:
            c.execute("INSERT OR REPLACE INTO keep(key, at) VALUES (?, ?)", (key, time.time()))
            # a kept title can't sit on the drop list
            c.execute("DELETE FROM shortlist WHERE title_key=?", (key,))
        else:
            c.execute("DELETE FROM keep WHERE key=?", (key,))


def log_deletion(d):
    with conn() as c:
        c.execute("INSERT INTO deletions(at,kind,key,title_key,label,bytes,ok,plex,arr,meta) VALUES (?,?,?,?,?,?,?,?,?,?)",
                  (time.time(), d["kind"], d["key"], d["title_key"], d["label"], int(d["bytes"]), 1 if d["ok"] else 0,
                   d.get("plex"), d.get("arr"), json.dumps(d.get("meta") or {}, ensure_ascii=False)))


def deletions(limit=500):
    out = rows("SELECT * FROM deletions ORDER BY at DESC LIMIT ?", limit)
    for r in out:
        r["meta"] = json.loads(r["meta"] or "{}")
    return out


def record_capacity(array, indexed, reason):
    if not array:
        return
    with conn() as c:
        c.execute("INSERT OR REPLACE INTO capacity(at,total,used,free,indexed,reason) VALUES (?,?,?,?,?,?)",
                  (time.time(), array["total"], array["used"], array["free"], indexed, reason))


def capacity_history(days=180):
    return rows("SELECT at,total,used,free,indexed,reason FROM capacity WHERE at > ? ORDER BY at",
                time.time() - days * 86400)


DG_COLS = ("created", "updated", "app", "item_id", "title_key", "season", "label", "target", "release", "quality",
           "indexer", "new_size", "old_size", "old_quality", "old_profile", "old_file_id", "old_file_ids", "state",
           "note", "final_size", "folder", "section", "old_path")


def dg_add(row):
    now = time.time()
    row = {**row, "created": now, "updated": now}
    with conn() as c:
        cur = c.execute(f"INSERT INTO downgrades({','.join(DG_COLS)}) VALUES ({','.join('?' * len(DG_COLS))})",
                        [row.get(k) for k in DG_COLS])
        return cur.lastrowid


def dg_update(id_, **kw):
    kw["updated"] = time.time()
    with conn() as c:
        c.execute(f"UPDATE downgrades SET {','.join(f'{k}=?' for k in kw)} WHERE id=?", [*kw.values(), id_])


def downgrades(where="1=1", *args):
    return rows(f"SELECT * FROM downgrades WHERE {where} ORDER BY created DESC", *args)
