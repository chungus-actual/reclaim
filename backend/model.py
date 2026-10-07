"""Pure model build: trimmed upstream data (+ optional disk walk) -> the page's model.

Things this relies on:
- Plex's Part.size is the file's size on disk, so sizes come from the index, not a disk scan.
- One file can back several episodes (multi-episode files). Summing per-episode sizes
  overcounts, so bytes are always deduped by file path.
- Tautulli (when configured) is the play log: partial plays, durations, users. Plex's own
  history (completed views only) fills in before Tautulli's first row, or everything when
  Tautulli isn't used. Plex calls the server owner account 1.
"""
import os
import re
import time
from collections import defaultdict

RES_RANK = {"4k": 6, "2160": 6, "1080": 5, "720": 4, "576": 3, "480": 2, "sd": 1}

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".mpg", ".mpeg", ".mov", ".wmv", ".flv", ".webm",
             ".divx", ".ogm", ".m2v", ".3gp", ".rmvb", ".asf"}
DISC_EXT = {".iso", ".img", ".m2ts", ".vob", ".bup", ".ifo", ".mts"}
SIDECAR_EXT = {".srt", ".sub", ".idx", ".ass", ".ssa", ".smi", ".vtt", ".sup", ".nfo", ".nfo-orig", ".jpg",
               ".jpeg", ".png", ".tbn", ".txt", ".srr", ".sfv", ".md5", ".url", ".xml", ".db", ".ini", ".json",
               ".log", ".exe", ".htm", ".html", ".bif", ".sha1"}
SHORT_NAME = re.compile(r"/[A-Z0-9_]{1,6}~[0-9A-Z](/|$)")   # SMB 8.3 alias for an unrepresentable name
TEMP_NAME = re.compile(r"(^\.fuse_hidden|\.part$|\.partial$|\.!qb$|\.tmp$|^\..+\.[a-z0-9]{2,4}\.[A-Za-z0-9]{6}$)", re.I)

CATEGORIES = {
    "temp": "Temp / interrupted transfer",
    "disc": "Disc image or rip Plex can't play",
    "video": "Video Plex didn't index",
    "other": "Other large file",
    "sidecar": "Sidecar (subs, nfo, art)",
    "alias": "Indexed, but its name is unreadable over SMB",
}


def norm(s):
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def res_label(r):
    if not r:
        return None
    r = str(r).lower()
    return "4K" if r in ("4k", "2160") else (r.upper() if r == "sd" else f"{r}p")


def classify(path, size):
    base = path.rsplit("/", 1)[-1]
    ext = os.path.splitext(base)[1].lower()
    if SHORT_NAME.search(path):
        return "alias"
    if TEMP_NAME.search(base):
        return "temp"
    if ext in DISC_EXT or "/BDMV/" in path or "/VIDEO_TS/" in path:
        return "disc"
    if ext in VIDEO_EXT:
        return "video"
    if ext in SIDECAR_EXT or size < 50_000_000:
        return "sidecar"
    return "other"


class Model:
    """Everything the API serves, built in one pass. Immutable once built."""

    def __init__(self, raw, walk=None, keep=frozenset(), extras=None):
        t0 = time.time()
        self.generated = raw.get("fetched_at") or time.time()
        self.notes = list(raw.get("notes") or [])
        self.sections = {s["id"]: s for s in raw["sections"]}
        self.array = raw.get("array")
        self.server = raw.get("server") or {}
        self._users(raw)
        self._titles(raw)
        self._extras(extras or {})
        self._arr(raw.get("arr") or {})
        self._requests(raw.get("requests") or [])
        self._plays(raw)
        self._walk(walk)
        self.keep = set(keep)
        self.build_ms = int((time.time() - t0) * 1000)

    # ------------------------------------------------------------- users
    def _users(self, raw):
        self.users = {}
        for u in raw.get("users") or []:
            self.users[int(u["id"])] = {"id": int(u["id"]), "name": u["name"], "admin": u["admin"], "home": u["home"]}
        self.admin = next((u["id"] for u in self.users.values() if u["admin"]), None)

    def _uid(self, uid):
        uid = int(uid)
        if uid not in self.users:
            self.users[uid] = {"id": uid, "name": f"user {uid}", "admin": False, "home": False}
        return uid

    # ------------------------------------------------------------ titles
    def _root_of(self, sec, path):
        for r in self.sections[sec]["roots"]:
            r = r.rstrip("/") + "/"
            if path.startswith(r):
                return r
        return os.path.dirname(path) + "/"

    def _folder(self, sec, path):
        """Top-level folder under the library root (the title's directory)."""
        root = self._root_of(sec, path)
        head = path[len(root):].split("/", 1)
        return root + head[0] if len(head) > 1 else path

    def _titles(self, raw):
        self.titles = {}           # key -> title record
        self.files = {}            # file path -> (title key, season key | None)
        self.folders = defaultdict(set)   # folder -> {title keys}
        self.seasons = {}          # season key -> season record
        self.episodes = {}         # episode key -> (show key, season key, sindex, index, title, duration)
        self.multi_ep_bytes = 0
        self.lib_bytes = defaultdict(int)

        for m in raw["movies"]:
            files, versions = {}, []
            for md in m["media"]:
                vb = 0
                for p in md["parts"]:
                    if p["file"]:
                        files[p["file"]] = p["size"]
                        vb += p["size"]
                versions.append({"id": md["id"], "res": res_label(md["res"]), "vcodec": md["vcodec"],
                                 "acodec": md["acodec"], "bitrate": md["bitrate"], "container": md["container"],
                                 "bytes": vb, "files": [p["file"] for p in md["parts"]]})
            best = max(versions, key=lambda v: RES_RANK.get((v["res"] or "").lower().rstrip("p"), 0), default=None)
            first = next(iter(files), None)
            folder = self._folder(m["section"], first) if first else None
            t = {"key": m["key"], "kind": "movie", "section": m["section"], "title": m["title"], "year": m["year"],
                 "added": m["added"], "size": sum(files.values()), "files": len(files), "versions": versions,
                 "res": best["res"] if best else None, "vcodec": best["vcodec"] if best else None,
                 "bitrate": best["bitrate"] if best else None,
                 "duration": m["duration"], "rating": m["rating"], "content": m["content"], "genres": m["genres"],
                 "thumb": m["thumb"], "ids": m["ids"], "folder": folder, "edition": m.get("edition")}
            self.titles[m["key"]] = t
            for f in files:
                self.files[f] = (m["key"], None)
            if folder:
                self.folders[folder].add(m["key"])
            self.lib_bytes[m["section"]] += t["size"]

        shows = {s["key"]: s for s in raw["shows"]}
        for s in raw["seasons"]:
            self.seasons[s["key"]] = {"key": s["key"], "show": s["show"], "index": s["index"], "title": s["title"],
                                      "size": 0, "eps": 0, "files": {}}
        show_files = defaultdict(dict)
        show_eps = defaultdict(int)
        show_res = defaultdict(lambda: defaultdict(int))
        for e in raw["episodes"]:
            sk, shk = e["season"], e["show"]
            self.episodes[e["key"]] = (shk, sk, e["sindex"], e["index"], e["title"], e["duration"])
            show_eps[shk] += 1
            se = self.seasons.get(sk)
            if se is None:
                se = self.seasons[sk] = {"key": sk, "show": shk, "index": e["sindex"], "title": f"Season {e['sindex']}",
                                         "size": 0, "eps": 0, "files": {}}
            se["eps"] += 1
            se.setdefault("res", {})
            for md in e["media"]:
                for p in md["parts"]:
                    f = p["file"]
                    if not f:
                        continue
                    if f in show_files[shk]:
                        self.multi_ep_bytes += p["size"]
                    show_files[shk][f] = p["size"]
                    se["files"][f] = p["size"]
                    self.files[f] = (shk, sk)
                    show_res[shk][res_label(md["res"])] += p["size"]
                    se["res"][res_label(md["res"])] = se["res"].get(res_label(md["res"]), 0) + p["size"]
        for se in self.seasons.values():
            se["size"] = sum(se["files"].values())

        for k, s in shows.items():
            files = show_files.get(k, {})
            first = next(iter(files), None)
            folder = self._folder(s["section"], first) if first else None
            res = max(show_res[k].items(), key=lambda kv: kv[1])[0] if show_res[k] else None
            t = {"key": k, "kind": "show", "section": s["section"], "title": s["title"], "year": s["year"],
                 "added": s["added"], "size": sum(files.values()), "files": len(files), "res": res,
                 "eps": show_eps.get(k, 0), "nseasons": sum(1 for x in self.seasons.values() if x["show"] == k),
                 "rating": s["rating"], "content": s["content"], "genres": s["genres"], "thumb": s["thumb"],
                 "ids": s["ids"], "folder": folder, "res_mix": {r: b for r, b in show_res[k].items() if r}}
            self.titles[k] = t
            if folder:
                self.folders[folder].add(k)
            self.lib_bytes[s["section"]] += t["size"]

    # ------------------------------------------------------------ extras
    def _extras(self, extras):
        """Local extras Plex indexed (Featurettes/, *-trailer.mkv, ...), keyed by the title or
        season they hang off. They aren't title bytes (a Plex delete of the title isn't known to
        take them), but they are in Plex, so the walk mustn't call them unindexed."""
        self.extra_files = {}      # file path -> title key
        for t in self.titles.values():
            t["extras"] = []
        for parent, clips in extras.items():
            key = self.seasons[parent]["show"] if parent in self.seasons else parent
            if key not in self.titles:
                continue           # title gone from Plex since the lookup, and its extras with it
            for c in clips:
                for path, size in c["files"]:
                    if path not in self.files and path not in self.extra_files:
                        self.extra_files[path] = key
                        self.titles[key]["extras"].append((path, size, c.get("subtype") or "extra"))

    # --------------------------------------------------------------- arr
    def _arr(self, arr):
        self.arr_instances = arr.get("instances") or {}
        self.arr_safety = {}
        for app, key in (("radarr", "autoUnmonitorPreviouslyDownloadedMovies"),
                         ("sonarr", "autoUnmonitorPreviouslyDownloadedEpisodes")):
            mm = arr.get(f"{app}_mm")
            if mm is not None:
                self.arr_safety[app] = bool(mm.get(key))
        rprof, sprof = arr.get("radarr_profiles") or {}, arr.get("sonarr_profiles") or {}
        by_dir = {"radarr": {}, "sonarr": {}}
        by_id = {"radarr": {}, "sonarr": {}}
        for m in arr.get("radarr") or []:
            by_dir["radarr"][(m["path"] or "").rstrip("/").rsplit("/", 1)[-1]] = m
            if m["tmdb"]:
                by_id["radarr"][str(m["tmdb"])] = m
        for s in arr.get("sonarr") or []:
            by_dir["sonarr"][(s["path"] or "").rstrip("/").rsplit("/", 1)[-1]] = s
            if s["tvdb"]:
                by_id["sonarr"][str(s["tvdb"])] = s
        self.arr_matched = defaultdict(int)
        for t in self.titles.values():
            app = "radarr" if t["kind"] == "movie" else "sonarr"
            # folder is the file itself when a movie sits loose in the library root
            folder_is_dir = t["folder"] and t["folder"] not in self.files
            x = by_dir[app].get(t["folder"].rsplit("/", 1)[-1]) if folder_is_dir else None
            if x is None:
                x = by_id[app].get(t["ids"].get("tmdb" if app == "radarr" else "tvdb", ""))
            if x is None:
                t["arr"] = None
                continue
            self.arr_matched[app] += 1
            t["arr"] = {"app": app, "id": x["id"], "monitored": bool(x["monitored"]),
                        "profile": (rprof if app == "radarr" else sprof).get(x["profile"])}
            if app == "radarr":
                t["arr"]["file"] = x.get("file")
            else:
                t["arr"]["seasons"] = x.get("seasons") or {}
                t["arr"]["status"] = x.get("status")

    # ---------------------------------------------------------- requests
    def _requests(self, reqs):
        by_key = {t["key"]: t for t in self.titles.values()}
        ids = {"movie": defaultdict(list), "show": defaultdict(list)}
        for t in self.titles.values():
            for scheme in ("tmdb", "tvdb"):
                if t["ids"].get(scheme):
                    ids[t["kind"]][(scheme, t["ids"][scheme])].append(t)
        for t in self.titles.values():
            t["requests"] = []
        for q in reqs:
            kind = "movie" if q["type"] == "movie" else "show"
            hits = []
            if q["rating_key"] and str(q["rating_key"]) in by_key and by_key[str(q["rating_key"])]["kind"] == kind:
                hits = [by_key[str(q["rating_key"])]]
            if not hits and kind == "show" and q["tvdb"]:
                hits = ids[kind].get(("tvdb", str(q["tvdb"])), [])
            if not hits and q["tmdb"]:
                hits = ids[kind].get(("tmdb", str(q["tmdb"])), [])
            uid = self._uid(q["plex_id"]) if q["plex_id"] else None
            if uid is not None and self.users[uid]["name"].startswith("user ") and q["by"]:
                self.users[uid]["name"] = q["by"]
            for t in hits[:1]:
                t["requests"].append({"uid": uid, "by": q["by"], "at": q["at"], "status": q["status"],
                                      "seasons": q["seasons"]})

    # ------------------------------------------------------------- plays
    def _plays(self, raw):
        movies_by_ty = {}
        for t in self.titles.values():
            if t["kind"] == "movie":
                movies_by_ty.setdefault((norm(t["title"]), t["year"]), t["key"])
        shows_by_title = {}
        for t in self.titles.values():
            if t["kind"] == "show":
                shows_by_title.setdefault(norm(t["title"]), t["key"])
        season_by_idx = {(s["show"], s["index"]): k for k, s in self.seasons.items()}

        # (title, season, episode, uid, ts, secs, finished, pct)
        plays = []
        self.unmatched_plays = 0
        th = raw.get("tautulli_history") or []
        self.tautulli_since = min((r["date"] for r in th if r.get("date")), default=None)
        for r in th:
            mt = r["media_type"]
            uid = self._uid(r["user_id"] or 0)
            ts = r["stopped"] or r["date"]
            secs = r["play_duration"] or 0
            pct = r["percent_complete"] or 0
            fin = r["watched_status"] == 1
            if mt == "movie":
                k = str(r["rating_key"])
                if k not in self.titles or self.titles[k]["kind"] != "movie":
                    k = movies_by_ty.get((norm(r["title"]), r["year"]))
                if not k:
                    self.unmatched_plays += 1
                    continue
                plays.append((k, None, None, uid, ts, secs, fin, pct))
            elif mt == "episode":
                ek = str(r["rating_key"])
                if ek in self.episodes:
                    shk, sk = self.episodes[ek][0], self.episodes[ek][1]
                else:
                    ek = None
                    shk = str(r["grandparent_rating_key"])
                    if shk not in self.titles or self.titles[shk]["kind"] != "show":
                        shk = shows_by_title.get(norm(r["grandparent_title"]))
                    sk = season_by_idx.get((shk, r["parent_media_index"])) if shk else None
                if not shk:
                    self.unmatched_plays += 1
                    continue
                plays.append((shk, sk, ek, uid, ts, secs, fin, pct))

        self.plex_since = None
        pre = 0
        for h in raw.get("plex_history") or []:
            if not h["at"]:
                continue
            self.plex_since = h["at"] if self.plex_since is None else min(self.plex_since, h["at"])
            if self.tautulli_since and h["at"] >= self.tautulli_since:
                continue
            acct = h["account"]
            uid = self._uid(self.admin if acct == 1 and self.admin else (acct or 0))
            k = h["key"]
            if not k:
                continue
            if k in self.titles and self.titles[k]["kind"] == "movie":
                secs = (self.titles[k]["duration"] or 0) // 1000
                plays.append((k, None, None, uid, h["at"], secs, True, 100))
            elif k in self.episodes:
                shk, sk, _, _, _, dur = self.episodes[k]
                plays.append((shk, sk, k, uid, h["at"], (dur or 0) // 1000, True, 100))
            else:
                continue
            pre += 1
        self.pre_tautulli_plays = pre

        self.plays = plays
        self.plays_by_title = defaultdict(list)
        agg = defaultdict(lambda: [0, 0, 0, 0, 0, set()])  # plays, fin, lastAny, lastFin, secs, eps finished
        for p in plays:
            self.plays_by_title[p[0]].append(p)
            a = agg[(p[0], p[3])]
            a[0] += 1
            a[4] += p[5]
            if p[4] and p[4] > a[2]:
                a[2] = p[4]
            if p[6]:
                a[1] += 1
                if p[4] and p[4] > a[3]:
                    a[3] = p[4]
                if p[2]:
                    a[5].add(p[2])
        for t in self.titles.values():
            t["users"] = {}
        for (k, uid), a in agg.items():
            self.titles[k]["users"][uid] = (a[0], a[1], a[2], a[3], a[4], len(a[5]))
        # distinct episodes finished by anyone
        fin_eps = defaultdict(set)
        for p in plays:
            if p[6] and p[2]:
                fin_eps[p[0]].add(p[2])
        for t in self.titles.values():
            if t["kind"] == "show":
                t["eps_seen"] = len(fin_eps.get(t["key"], ()))

    # -------------------------------------------------------------- walk
    def _walk(self, walk):
        self.walk_at = None
        self.unindexed = []
        self.unindexed_by_cat = defaultdict(lambda: [0, 0])
        self.missing_on_disk = 0
        self.extras_on_disk = [0, 0]     # files, bytes of Plex-indexed extras the walk saw
        self.extra_owners = set()        # titles with video in their folder the index doesn't account for
        for t in self.titles.values():
            t["xbytes"] = 0
        if not walk:
            return
        self.walk_at = walk.get("at")
        roots = [r.rstrip("/") + "/" for s in self.sections.values() for r in s["roots"]]
        sec_of_root = {r.rstrip("/") + "/": sid for sid, s in self.sections.items() for r in s["roots"]}
        seen = set()
        groups = defaultdict(lambda: {"bytes": 0, "files": []})
        for path, size in walk.get("files") or []:
            root = next((r for r in roots if path.startswith(r)), None)
            if root is None:
                continue
            if path in self.files:
                seen.add(path)
                continue
            cat = classify(path, size)
            head = path[len(root):].split("/", 1)
            folder = root + head[0] if len(head) > 1 else path
            # decided before the extras check, so a lookup's answer can't change who gets asked next time
            if cat in ("video", "disc", "other"):
                self.extra_owners.update(self.folders.get(folder, ()))
            if path in self.extra_files:
                self.extras_on_disk[0] += 1
                self.extras_on_disk[1] += size
                continue
            self.unindexed_by_cat[cat][0] += 1
            self.unindexed_by_cat[cat][1] += size
            g = groups[folder]
            g["bytes"] += size if cat not in ("sidecar", "alias") else 0
            g["files"].append((path, size, cat))
            g["section"] = sec_of_root[root]
        walked = [r.rstrip("/") + "/" for r in (walk.get("roots") or roots)]
        # Indexed files the walk didn't see: deleted since the walk, or a name SMB
        # can't represent (trailing-space folders surface as 8.3 aliases instead).
        self.missing_on_disk = sum(1 for f in self.files if f not in seen and any(f.startswith(r) for r in walked))
        for folder, g in groups.items():
            owners = sorted(self.folders.get(folder, ()))
            for k in owners[:1]:
                self.titles[k]["xbytes"] += g["bytes"]
            cats = defaultdict(int)
            for _, s, c in g["files"]:
                cats[c] += s
            self.unindexed.append({"folder": folder, "section": g["section"], "title": owners[0] if owners else None,
                                   "bytes": g["bytes"], "cats": dict(cats),
                                   "files": sorted(g["files"], key=lambda f: -f[1])})
        self.unindexed.sort(key=lambda g: -g["bytes"])

    def extra_candidates(self):
        """Plex keys to ask for local extras: titles whose folder holds video the index doesn't
        account for, plus those shows' seasons so an extra Plex filed under a season isn't missed.
        Plex only returns extras inside their parent's metadata, so asking about every title
        would cost minutes per refresh (each movie drags in its online trailers too)."""
        keys = set(self.extra_owners)
        shows = {k for k in self.extra_owners if self.titles[k]["kind"] == "show"}
        if shows:
            keys.update(s["key"] for s in self.seasons.values() if s["show"] in shows)
        return sorted(keys)

    # ----------------------------------------------------------- outputs
    def client(self):
        """Compact payload for the page. Per-user rows let the page re-lens instantly."""
        user_plays = defaultdict(int)
        for p in self.plays:
            user_plays[p[3]] += 1
        users = sorted(self.users.values(), key=lambda u: -user_plays.get(u["id"], 0))
        titles = []
        for t in self.titles.values():
            row = {"k": t["key"], "m": 1 if t["kind"] == "movie" else 0, "s": t["section"], "n": t["title"],
                   "y": t["year"], "a": t["added"], "z": t["size"], "f": t["files"], "x": t["xbytes"],
                   "r": t["res"], "g": t["genres"][:3], "th": t["thumb"], "kp": 1 if t["key"] in self.keep else 0,
                   "u": [[uid, *v] for uid, v in t["users"].items()],
                   "q": [[r["uid"], r["at"]] for r in t["requests"]]}
            if t["kind"] == "movie":
                row["v"] = len(t["versions"])
                row["d"] = round((t["duration"] or 0) / 60000)
            else:
                row["e"] = t["eps"]
                row["es"] = t["eps_seen"]
                row["sn"] = t["nseasons"]
            if t.get("arr"):
                row["ar"] = [1 if t["arr"]["monitored"] else 0, t["arr"]["profile"]]
            titles.append(row)
        a = self.array
        indexed = sum(self.lib_bytes.values())
        unindexed = sum(v[1] for c, v in self.unindexed_by_cat.items() if c not in ("alias",))
        return {
            "generated": self.generated, "build_ms": self.build_ms, "server": self.server,
            "sections": [{"id": s["id"], "title": s["title"], "type": s["type"], "bytes": self.lib_bytes.get(s["id"], 0)}
                         for s in self.sections.values()],
            "array": a,
            "space": {"indexed": indexed, "unindexed": unindexed if self.walk_at else None,
                      "multi_ep_dedup": self.multi_ep_bytes},
            "users": [{"id": u["id"], "n": u["name"], "ad": 1 if u["admin"] else 0, "pl": user_plays.get(u["id"], 0)}
                      for u in users],
            "history": {"tautulli_since": self.tautulli_since, "plex_since": self.plex_since,
                        "pre_tautulli": self.pre_tautulli_plays, "plays": len(self.plays),
                        "unmatched": self.unmatched_plays},
            "arr": {"safety": self.arr_safety, "matched": dict(self.arr_matched)},
            "walk_at": self.walk_at,
            "notes": self.notes,
            "titles": titles,
        }

    def detail(self, key):
        t = self.titles.get(key)
        if not t:
            return None
        name = lambda uid: self.users.get(uid, {}).get("name", str(uid))
        users = sorted(({"uid": uid, "name": name(uid), "plays": v[0], "fin": v[1], "last": v[2], "last_fin": v[3],
                         "hours": round(v[4] / 3600, 1), "eps": v[5]} for uid, v in t["users"].items()),
                       key=lambda u: -(u["last"] or 0))
        recent = []
        for p in sorted(self.plays_by_title.get(key, []), key=lambda p: -(p[4] or 0))[:30]:
            ep = self.episodes.get(p[2]) if p[2] else None
            label = f"S{ep[2]:02d}E{ep[3]:02d} {ep[4]}" if ep and ep[2] is not None and ep[3] is not None else None
            if not label and p[1] in self.seasons:
                label = self.seasons[p[1]]["title"]
            recent.append({"user": name(p[3]), "at": p[4], "pct": p[7], "fin": p[6], "mins": round(p[5] / 60), "ep": label})
        out = {k: t[k] for k in ("key", "kind", "section", "title", "year", "added", "size", "files", "res", "rating",
                                 "content", "genres", "folder", "ids", "xbytes")}
        out.update({"users": users, "recent": recent, "arr": t.get("arr"),
                    "requests": [dict(r, name=name(r["uid"]) if r["uid"] else r["by"]) for r in t["requests"]],
                    "kept": key in self.keep, "extras": sorted(t["extras"], key=lambda f: -f[1]),
                    "unindexed": next((g for g in self.unindexed if g["folder"] == t["folder"]), None)})
        if t["kind"] == "movie":
            out["versions"] = t["versions"]
            out["duration"] = t["duration"]
            out["edition"] = t.get("edition")
        else:
            agg = defaultdict(lambda: {"plays": 0, "users": set(), "last": 0, "fin_eps": set()})
            for p in self.plays_by_title.get(key, []):
                a = agg[p[1]]
                a["plays"] += 1
                a["users"].add(p[3])
                a["last"] = max(a["last"], p[4] or 0)
                if p[6] and p[2]:
                    a["fin_eps"].add(p[2])
            seasons = []
            mon = (t.get("arr") or {}).get("seasons") or {}
            for s in sorted((s for s in self.seasons.values() if s["show"] == key), key=lambda s: (s["index"] is None, s["index"])):
                a = agg.get(s["key"], {"plays": 0, "users": set(), "last": 0, "fin_eps": set()})
                seasons.append({"key": s["key"], "index": s["index"], "title": s["title"], "eps": s["eps"],
                                "size": s["size"], "plays": a["plays"], "viewers": len(a["users"]),
                                "viewer_names": sorted(name(u) for u in a["users"]), "last": a["last"] or None,
                                "eps_seen": len(a["fin_eps"]), "monitored": mon.get(str(s["index"])),
                                "res": max((s.get("res") or {}).items(), key=lambda kv: kv[1])[0] if s.get("res") else None})
            out["seasons"] = seasons
            out["eps"] = t["eps"]
            out["eps_seen"] = t["eps_seen"]
            out["res_mix"] = t["res_mix"]
        return out
