"""Write a made-up library for trying reclaim (and for screenshots) without connecting anything.

  python tools/demo_data.py --data data-demo
  cd backend && RECLAIM_DEMO=1 RECLAIM_DATA=../data-demo uvicorn main:app --port 8892

Everything is invented: titles, people, plays, requests, Radarr/Sonarr records, the array, past deletions
and downgrades. Demo mode is read-only and talks to no service. Never point --data at a real data folder.
"""
import argparse
import datetime as dt
import gzip
import json
import os
import random
import sqlite3
import sys
import time
from pathlib import Path

GB = 1_000_000_000
DAY = 86400
ADJ = ["Silent", "Copper", "Glass", "Hollow", "Northern", "Paper", "Velvet", "Iron", "Last", "Second", "Quiet", "Burning",
       "Lucky", "Electric", "Crooked", "Golden", "Distant", "Midnight", "Wild", "Little", "Salt", "Winter", "Broken", "Bright"]
NOUN = ["Harbor", "Orchard", "Lantern", "Cartographer", "Signal", "Kingdom", "Garden", "Frontier", "Engine", "Season",
        "Witness", "Tide", "Archive", "Comet", "Ferry", "Balcony", "Canyon", "Parade", "Machine", "Island", "Verdict",
        "Carnival", "Lighthouse", "Telegram", "Meridian", "Alibi"]
SHOW_A = ["Northbound", "Low Tide", "The Night Desk", "Field Notes", "Paper Moon Street", "Kettle Hill", "Small Hours",
          "The Cartographers", "Second Shift", "Glasshouse", "Dry County", "The Long Table", "Station Eleven Road",
          "Saltmarsh", "The Understudy", "Ridgeback", "Copper Valley", "Open Water", "The Archivists", "Lamplighters",
          "Midnight Ferry", "The Quiet Year", "Westward", "Clockwork Lane", "Harbor Lights", "Bright Young Things",
          "The Pantry", "Tin Star", "Outer Banks Radio", "Long Division"]
GENRES = ["Drama", "Comedy", "Thriller", "Science Fiction", "Documentary", "Animation", "Adventure", "Mystery", "Romance", "Horror"]
USERS = [("alex", True), ("jordan", False), ("sam", False), ("riley", False), ("casey", False), ("morgan", False), ("taylor", False)]
RES = [("4k", .18), ("1080", .6), ("720", .14), ("sd", .08)]
SIZE = {"4k": (14, 78), "1080": (3.5, 19), "720": (1.6, 5.5), "sd": (.6, 1.8)}
EP_SIZE = {"4k": (4, 11), "1080": (.9, 3.8), "720": (.4, 1.3), "sd": (.2, .6)}
CODEC = {"4k": ["hevc"], "1080": ["h264", "hevc", "h264", "av1"], "720": ["h264"], "sd": ["mpeg4", "h264"]}


def pick(rnd, weighted):
    r, acc = rnd.random(), 0
    for v, w in weighted:
        acc += w
        if r <= acc:
            return v
    return weighted[-1][0]


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def media(rnd, mid, res, files, size_range):
    lo, hi = size_range
    parts = [{"file": f, "size": int(rnd.uniform(lo, hi) * GB)} for f in files]
    kbps = int(sum(p["size"] for p in parts) * 8 / 1000 / (rnd.randint(85, 165) * 60))
    return {"id": mid, "res": res, "vcodec": rnd.choice(CODEC[res]), "acodec": rnd.choice(["eac3", "truehd", "aac", "dts", "ac3"]),
            "bitrate": kbps, "container": rnd.choice(["mkv", "mkv", "mp4"]), "height": {"4k": 2160, "1080": 1080, "720": 720, "sd": 480}[res],
            "parts": parts}


def build(seed=7):
    rnd = random.Random(seed)
    now = time.time()
    users = [{"id": 1001 + i, "name": n, "username": n, "admin": a, "home": False, "active": True} for i, (n, a) in enumerate(USERS)]
    uw = [(u["id"], w) for u, w in zip(users, [.34, .17, .14, .12, .1, .08, .05])]
    sections = [{"id": "1", "type": "movie", "title": "Movies", "roots": ["/data/movies"]},
                {"id": "2", "type": "show", "title": "TV Shows", "roots": ["/data/tv"]}]
    nextkey = iter(range(20000, 99999))
    movies, seen = [], set()
    while len(movies) < 150:
        title = f"{rnd.choice(['The ', '', ''])}{rnd.choice(ADJ)} {rnd.choice(NOUN)}"
        if title in seen:
            continue
        seen.add(title)
        key, year = str(next(nextkey)), rnd.randint(1972, 2025)
        res = pick(rnd, RES)
        folder = f"/data/movies/{title} ({year})"
        vers = [media(rnd, int(next(nextkey)), res, [f"{folder}/{title} ({year}) - {res}.mkv"], SIZE[res])]
        if res == "4k" and rnd.random() < .45:                       # some 4K titles also keep a 1080p copy
            vers.append(media(rnd, int(next(nextkey)), "1080", [f"{folder}/{title} ({year}) - 1080p.mkv"], SIZE["1080"]))
        movies.append({"key": key, "title": title, "year": year, "added": int(now - rnd.uniform(20, 3200) * DAY),
                       "duration": rnd.randint(84, 172) * 60000, "rating": round(rnd.uniform(5.2, 9.1), 1),
                       "content": rnd.choice(["PG", "PG-13", "R", "G", "TV-MA"]), "genres": rnd.sample(GENRES, 2),
                       "thumb": f"/library/metadata/{key}/thumb/1", "ids": {"tmdb": str(400000 + int(key)), "imdb": f"tt9{key}"},
                       "media": vers, "edition": None, "section": "1"})
    shows, seasons, episodes = [], [], []
    for name in SHOW_A:
        key, year = str(next(nextkey)), rnd.randint(1998, 2025)
        res = pick(rnd, [("4k", .12), ("1080", .66), ("720", .16), ("sd", .06)])
        added = int(now - rnd.uniform(40, 3000) * DAY)
        shows.append({"key": key, "title": name, "year": year, "added": added, "rating": round(rnd.uniform(6, 9.2), 1),
                      "content": rnd.choice(["TV-14", "TV-MA", "TV-PG"]), "genres": rnd.sample(GENRES, 2),
                      "thumb": f"/library/metadata/{key}/thumb/1", "ids": {"tvdb": str(300000 + int(key)), "tmdb": str(700000 + int(key))},
                      "leafs": 0, "seasons": 0, "section": "2"})
        for sn in range(1, rnd.randint(1, 7) + 1):
            skey = str(next(nextkey))
            seasons.append({"key": skey, "show": key, "index": sn, "title": f"Season {sn}", "thumb": f"/library/metadata/{skey}/thumb/1"})
            for en in range(1, rnd.randint(6, 13) + 1):
                ekey = str(next(nextkey))
                f = f"/data/tv/{name}/Season {sn:02d}/{name} - S{sn:02d}E{en:02d}.mkv"
                episodes.append({"key": ekey, "season": skey, "show": key, "index": en, "sindex": sn,
                                 "title": f"{rnd.choice(ADJ)} {rnd.choice(NOUN)}", "added": added + sn * 30 * DAY,
                                 "duration": rnd.randint(22, 58) * 60000,
                                 "media": [media(rnd, int(next(nextkey)), res, [f], EP_SIZE[res])]})

    # plays: about a third of the library nobody has touched; the rest decays with age
    hist = []
    for m in movies:
        if rnd.random() < .34:
            continue
        for _ in range(rnd.choice([1, 1, 1, 2, 2, 3, 5, 8])):
            ts = rnd.uniform(m["added"], now) if rnd.random() < .5 else rnd.uniform(m["added"], m["added"] + (now - m["added"]) * .35)
            secs = m["duration"] // 1000
            pct = rnd.choice([100, 100, 100, 96, 64, 22])
            hist.append({"rating_key": m["key"], "parent_rating_key": None, "grandparent_rating_key": None, "media_type": "movie",
                         "user_id": pick(rnd, uw), "date": int(ts), "started": int(ts), "stopped": int(ts + secs * pct / 100),
                         "play_duration": int(secs * pct / 100), "percent_complete": pct, "watched_status": 1 if pct > 90 else 0,
                         "title": m["title"], "grandparent_title": None, "year": m["year"], "parent_media_index": None, "media_index": None})
    eps_by_show = {}
    for e in episodes:
        eps_by_show.setdefault(e["show"], []).append(e)
    for s in shows:
        if rnd.random() < .22:
            continue
        viewers = rnd.sample([u for u, _ in uw], rnd.randint(1, 3))
        eps = eps_by_show[s["key"]]
        for v in viewers:
            upto = rnd.randint(1, len(eps))
            start = rnd.uniform(s["added"], now)
            for i, e in enumerate(eps[:upto]):
                ts = min(now - DAY, start + i * rnd.uniform(.4, 3) * DAY)
                secs = e["duration"] // 1000
                hist.append({"rating_key": e["key"], "parent_rating_key": e["season"], "grandparent_rating_key": s["key"],
                             "media_type": "episode", "user_id": v, "date": int(ts), "started": int(ts), "stopped": int(ts + secs),
                             "play_duration": secs, "percent_complete": 100, "watched_status": 1, "title": e["title"],
                             "grandparent_title": s["title"], "year": s["year"], "parent_media_index": e["sindex"], "media_index": e["index"]})
    hist.sort(key=lambda r: r["date"])

    # requests: friends ask, and sometimes never watch what they asked for
    reqs = []
    for i, t in enumerate(rnd.sample(movies, 34) + rnd.sample(shows, 10)):
        u = rnd.choice(users[1:])
        tv = t["section"] == "2"
        reqs.append({"id": i + 1, "type": "tv" if tv else "movie", "status": 2, "at": iso(t["added"] - rnd.uniform(1, 6) * DAY),
                     "plex_id": u["id"], "by": u["name"], "tmdb": int(t["ids"]["tmdb"]), "tvdb": int(t["ids"]["tvdb"]) if tv else None,
                     "rating_key": t["key"], "seasons": [1] if tv else []})

    arr = {"instances": {"radarr": {"base": "http://radarr:7878/api/v3", "name": "Radarr"},
                         "sonarr": {"base": "http://sonarr:8989/api/v3", "name": "Sonarr"}},
           "radarr_profiles": {"4": "HD-1080p", "5": "Ultra-HD", "6": "Any"}, "sonarr_profiles": {"4": "HD-1080p", "6": "Any"},
           "radarr_mm": {"autoUnmonitorPreviouslyDownloadedMovies": False},
           "sonarr_mm": {"autoUnmonitorPreviouslyDownloadedEpisodes": False},
           "radarr": [{"id": i + 1, "title": m["title"], "year": m["year"], "tmdb": int(m["ids"]["tmdb"]),
                       "path": f"/data/movies/{m['title']} ({m['year']})", "monitored": rnd.random() < .8, "has_file": True,
                       "profile": "5" if m["media"][0]["res"] == "4k" else "4", "size": sum(p["size"] for v in m["media"] for p in v["parts"]),
                       "file": m["media"][0]["parts"][0]["file"].rsplit("/", 1)[1], "added": iso(m["added"])}
                      for i, m in enumerate(movies) if rnd.random() < .9],
           "sonarr": [{"id": i + 1, "title": s["title"], "tvdb": int(s["ids"]["tvdb"]), "path": f"/data/tv/{s['title']}",
                       "monitored": True, "profile": "4", "status": rnd.choice(["ended", "continuing"]),
                       "seasons": {str(se["index"]): True for se in seasons if se["show"] == s["key"]}, "added": iso(s["added"])}
                      for i, s in enumerate(shows)]}

    lib = sum(p["size"] for m in movies for v in m["media"] for p in v["parts"]) + \
        sum(p["size"] for e in episodes for v in e["media"] for p in v["parts"])
    total = int(lib * 1.11 / 1e12 + 1) * 1_000_000_000_000
    used = lib + int(total * .03)
    disks = [{"name": f"disk{i + 1}", "size": total // 6, "used": used // 6, "free": (total - used) // 6, "status": "DISK_OK"} for i in range(6)]
    array = {"state": "STARTED", "total": total, "used": used, "free": total - used, "disks": disks}

    oldest = min(movies, key=lambda m: m["added"])                      # Plex's own history starts with one early play
    plex_hist = [{"key": oldest["key"], "type": "movie", "account": 1, "at": oldest["added"] + DAY, "section": "1"}]
    raw = {"server": {"friendlyName": "demo", "version": "1.41.0", "machineIdentifier": "demo"}, "sections": sections,
           "movies": movies, "shows": shows, "seasons": seasons, "episodes": episodes, "plex_history": plex_hist,
           "tautulli_history": hist, "users": users, "requests": reqs, "arr": arr, "array": array, "notes": [],
           "fetched_at": now}

    # the disk walk: everything Plex has, plus what it never indexed
    files = [[p["file"], p["size"]] for m in movies for v in m["media"] for p in v["parts"]]
    files += [[p["file"], p["size"]] for e in episodes for v in e["media"] for p in v["parts"]]
    for m in rnd.sample(movies, 7):
        files.append([f"/data/movies/{m['title']} ({m['year']})/{m['title']}.iso", int(rnd.uniform(7, 46) * GB)])
    for m in rnd.sample(movies, 12):
        files.append([f"/data/movies/{m['title']} ({m['year']})/{m['title']} ({m['year']}).en.srt", rnd.randint(40, 120) * 1000])
    files += [["/data/movies/.incoming/Lucky Comet (2019).mkv.partial", int(6.2 * GB)],
              ["/data/movies/.incoming/.Paper Engine (2008).mkv.Wh3kq", int(2.4 * GB)],
              ["/data/movies/Unsorted/home video 2019.mkv", int(3.1 * GB)],
              ["/data/movies/Unsorted/BDMV/STREAM/00001.m2ts", int(22.5 * GB)],
              ["/data/tv/Low Tide/Season 02/Low Tide - S02 extras.mkv", int(1.4 * GB)]]
    walk = {"at": now, "roots": ["/data/movies", "/data/tv"], "files": files, "errors": [], "seconds": 14}
    return raw, walk, movies, users, rnd, total, used, lib


def write(data_dir: Path, seed=7):
    raw, walk, movies, users, rnd, total, used, lib = build(seed)
    data_dir.mkdir(parents=True, exist_ok=True)
    for name, obj in (("raw.json.gz", raw), ("walk.json.gz", walk)):
        with gzip.open(data_dir / name, "wt", encoding="utf-8") as f:
            json.dump(obj, f)
    os.environ["RECLAIM_DATA"] = str(data_dir)
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
    import store                                          # noqa: E402  (reads RECLAIM_DATA)
    store.init()
    now = time.time()
    with sqlite3.connect(data_dir / "reclaim.db") as c:
        c.execute("DELETE FROM capacity")
        c.execute("DELETE FROM deletions")
        c.execute("DELETE FROM downgrades")
        for d in range(150, -1, -1):                      # four months of the array filling up
            u = used - int(d * 0.0021 * total) + rnd.randint(-2, 2) * GB * 40
            c.execute("INSERT INTO capacity(at,total,used,free,indexed,reason) VALUES (?,?,?,?,?,?)",
                      (now - d * DAY, total, u, total - u, lib - d * int(0.0019 * total), "nightly"))
        gone = [("The Velvet Kingdom (2011)", 48.2, "movie", ["sam"], ["jordan"]), ("Saltmarsh · Season 3", 31.6, "season", [], ["riley"]),
                ("Electric Parade (1999)", 9.4, "movie", ["alex"], []), ("Hollow Verdict (2016)", 61.0, "movie", [], ["casey"])]
        for i, (label, gb, kind, viewers, req) in enumerate(gone):
            c.execute("INSERT INTO deletions(at,kind,key,title_key,label,bytes,ok,plex,arr,meta) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (now - (i * 9 + 3) * DAY, kind, str(90000 + i), str(90000 + i), label, int(gb * GB), 1, "deleted", "unmonitored",
                       json.dumps({"viewers": [{"name": v, "plays": 2} for v in viewers], "requested": [{"name": r} for r in req],
                                   "folder": f"/data/movies/{label}", "files": [f"/data/movies/{label}/{label}.mkv"]})))
        fours = [m for m in movies if m["media"][0]["res"] == "4k"][:2]
        for i, m in enumerate(fours):
            old = sum(p["size"] for p in m["media"][0]["parts"])
            state, new = ("imported", int(old * .21)) if i == 0 else ("grabbed", int(old * .24))
            c.execute("INSERT INTO downgrades(created,updated,app,item_id,title_key,season,label,target,release,quality,indexer,new_size,"
                      "old_size,old_quality,old_profile,old_file_id,old_file_ids,state,note,final_size,folder,section) VALUES "
                      "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (now - (6 - i * 5) * DAY, now - (5 - i * 5) * DAY, "radarr", i + 1, m["key"], None, f"{m['title']} ({m['year']})",
                       1080, f"{m['title'].replace(' ', '.')}.{m['year']}.1080p.BluRay.x264-DEMO", "Bluray-1080p", "demo-indexer",
                       new, old, "Remux-2160p", 5, 1000 + i, "[]", state, None, new if state == "imported" else None,
                       f"/data/movies/{m['title']} ({m['year']})", "1"))
    plays = len(raw["tautulli_history"])
    print(f"wrote {data_dir}: {len(raw['movies'])} movies, {len(raw['shows'])} shows, {len(raw['episodes'])} episodes, "
          f"{plays} plays by {len(users)} made-up people; array {used / 1e12:.1f} of {total / 1e12:.0f} TB used")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--data", required=True, help="a NEW folder for the demo (never your real data folder)")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    target = Path(a.data)
    if (target / "settings.json").exists():
        sys.exit(f"{target} has a settings.json: that looks like a real reclaim data folder. Pick an empty one.")
    write(target, a.seed)
