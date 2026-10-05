"""Thin async clients for every upstream reclaim reads from.

Each fetch trims the payload to the fields the model uses — the raw Plex
episode listing alone is ~100 MB of cast lists and artwork URLs.
"""
import asyncio
import json
import logging
import os
import shutil
import time

import httpx

import config as C

log = logging.getLogger("reclaim.sources")

PLEX_PAGE = 5000


def _ids(item):
    """Plex external ids, e.g. {'tmdb': '50373', 'tvdb': '45147', 'imdb': 'tt0382508'}."""
    out = {}
    for g in item.get("Guid") or []:
        scheme, _, val = (g.get("id") or "").partition("://")
        if scheme and val:
            out[scheme] = val
    return out


def _tags(item, key, n=4):
    return [t["tag"] for t in (item.get(key) or [])[:n]]


def _media(item):
    out = []
    for m in item.get("Media") or []:
        out.append({
            "id": m.get("id"),
            "res": m.get("videoResolution"),
            "vcodec": m.get("videoCodec"),
            "acodec": m.get("audioCodec"),
            "bitrate": m.get("bitrate"),
            "container": m.get("container"),
            "height": m.get("height"),
            "parts": [{"file": p.get("file"), "size": p.get("size") or 0} for p in (m.get("Part") or [])],
        })
    return out


class Sources:
    def __init__(self):
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=10.0))

    async def close(self):
        await self.http.aclose()

    # ---------------------------------------------------------------- plex
    def _plex_headers(self):
        h = {"Accept": "application/json", "X-Plex-Client-Identifier": "reclaim", "X-Plex-Product": "reclaim"}
        if C.PLEX_TOKEN:
            h["X-Plex-Token"] = C.PLEX_TOKEN
        return h

    async def plex(self, path, **params):
        r = await self.http.get(C.PLEX_URL + path, params=params, headers=self._plex_headers())
        r.raise_for_status()
        return r.json().get("MediaContainer", {})

    async def plex_paged(self, path, **params):
        """Plex caps nothing by default, but paging keeps each response bounded."""
        out, start = [], 0
        while True:
            mc = await self.plex(path, **params, **{"X-Plex-Container-Start": start, "X-Plex-Container-Size": PLEX_PAGE})
            rows = mc.get("Metadata") or []
            out.extend(rows)
            total = mc.get("totalSize", len(out))
            start += len(rows)
            if not rows or start >= total:
                return out

    async def plex_server(self):
        mc = await self.plex("/")
        return {k: mc.get(k) for k in ("friendlyName", "version", "machineIdentifier")}

    async def plex_sections(self):
        mc = await self.plex("/library/sections")
        return [{"id": d["key"], "type": d["type"], "title": d["title"],
                 "roots": [l["path"] for l in d.get("Location", [])]}
                for d in mc.get("Directory", []) if d["type"] in ("movie", "show")]

    async def plex_movies(self, sid):
        rows = await self.plex_paged(f"/library/sections/{sid}/all", type=1, includeGuids=1)
        return [{
            "key": m["ratingKey"], "title": m.get("title"), "year": m.get("year"),
            "added": m.get("addedAt"), "duration": m.get("duration"), "rating": m.get("audienceRating"),
            "content": m.get("contentRating"), "genres": _tags(m, "Genre"), "thumb": m.get("thumb"),
            "ids": _ids(m), "media": _media(m), "edition": m.get("editionTitle"),
        } for m in rows]

    async def plex_shows(self, sid):
        rows = await self.plex_paged(f"/library/sections/{sid}/all", type=2, includeGuids=1)
        return [{
            "key": s["ratingKey"], "title": s.get("title"), "year": s.get("year"),
            "added": s.get("addedAt"), "rating": s.get("audienceRating"), "content": s.get("contentRating"),
            "genres": _tags(s, "Genre"), "thumb": s.get("thumb"), "ids": _ids(s),
            "leafs": s.get("leafCount"), "seasons": s.get("childCount"),
        } for s in rows]

    async def plex_seasons(self, sid):
        rows = await self.plex_paged(f"/library/sections/{sid}/all", type=3)
        return [{"key": s["ratingKey"], "show": s.get("parentRatingKey"), "index": s.get("index"),
                 "title": s.get("title"), "thumb": s.get("thumb")} for s in rows]

    async def plex_episodes(self, sid):
        rows = await self.plex_paged(f"/library/sections/{sid}/all", type=4)
        return [{
            "key": e["ratingKey"], "season": e.get("parentRatingKey"), "show": e.get("grandparentRatingKey"),
            "index": e.get("index"), "sindex": e.get("parentIndex"), "title": e.get("title"),
            "added": e.get("addedAt"), "duration": e.get("duration"), "media": _media(e),
        } for e in rows]

    async def plex_users(self):
        """Server accounts, used when Tautulli isn't configured. Plex calls the owner account 1;
        everything else (Overseerr, Tautulli) knows the owner by their plex.tv id, so look it up."""
        mc = await self.plex("/accounts")
        owner = {}
        if C.PLEX_TOKEN:
            try:
                r = await self.http.get("https://plex.tv/api/v2/user", headers=self._plex_headers(), timeout=15)
                if r.status_code == 200:
                    owner = r.json()
            except Exception as ex:
                log.info("plex.tv owner lookup failed (%s); owner stays account 1", ex)
        out = []
        for a in mc.get("Account") or []:
            aid = a.get("id")
            if aid is None:
                continue
            if aid == 1:
                out.append({"id": owner.get("id") or 1, "name": owner.get("username") or a.get("name") or "owner",
                            "username": owner.get("username"), "admin": True, "home": False, "active": True})
            else:
                out.append({"id": aid, "name": a.get("name") or ("Local" if aid == 0 else f"user {aid}"),
                            "username": a.get("name"), "admin": False, "home": False, "active": True})
        return out

    async def plex_history(self):
        rows = await self.plex_paged("/status/sessions/history/all", sort="viewedAt:asc")
        return [{"key": h.get("ratingKey"), "type": h.get("type"), "account": h.get("accountID"),
                 "at": h.get("viewedAt"), "section": h.get("librarySectionID")} for h in rows]

    async def plex_delete(self, path):
        r = await self.http.delete(C.PLEX_URL + path, headers=self._plex_headers())
        return r.status_code, r.text[:300]

    async def plex_scan(self, section, path):
        """Partial scan of one folder (no *arr has a Plex connection, so nothing else tells Plex)."""
        r = await self.http.get(f"{C.PLEX_URL}/library/sections/{section}/refresh", params={"path": path},
                                headers=self._plex_headers())
        return r.status_code

    async def plex_exists(self, key):
        r = await self.http.get(f"{C.PLEX_URL}/library/metadata/{key}", headers=self._plex_headers())
        return r.status_code == 200

    async def plex_thumb(self, path, w=240, h=360):
        params = {"width": w, "height": h, "minSize": 1, "upscale": 1, "url": path}
        r = await self.http.get(C.PLEX_URL + "/photo/:/transcode", params=params, headers=self._plex_headers())
        r.raise_for_status()
        return r.content, r.headers.get("content-type", "image/jpeg")

    # ------------------------------------------------------------ tautulli
    async def tautulli(self, cmd, **params):
        r = await self.http.get(C.TAUTULLI_URL + "/api/v2", params={"apikey": C.TAUTULLI_API_KEY, "cmd": cmd, **params})
        r.raise_for_status()
        resp = r.json()["response"]
        if resp.get("result") != "success":
            raise RuntimeError(f"tautulli {cmd}: {resp.get('message')}")
        return resp["data"]

    async def tautulli_users(self):
        rows = await self.tautulli("get_users")
        return [{"id": u["user_id"], "name": u.get("friendly_name") or u.get("username"),
                 "username": u.get("username"), "admin": bool(u.get("is_admin")),
                 "home": bool(u.get("is_home_user")), "active": bool(u.get("is_active"))} for u in rows]

    async def tautulli_history(self):
        # grouping=1 folds resumed sessions into one play.
        data = await self.tautulli("get_history", length=500000, grouping=1)
        keep = ("rating_key", "parent_rating_key", "grandparent_rating_key", "media_type", "user_id",
                "date", "started", "stopped", "play_duration", "percent_complete", "watched_status",
                "title", "grandparent_title", "year", "parent_media_index", "media_index")
        return [{k: r.get(k) for k in keep} for r in data["data"]]

    # ------------------------------------------------- overseerr / jellyseerr
    def _ov(self):
        return {"X-Api-Key": C.SEERR_API_KEY}

    async def seerr_requests(self):
        out, skip = [], 0
        while True:
            r = await self.http.get(C.SEERR_URL + "/api/v1/request",
                                    params={"take": 100, "skip": skip, "filter": "all", "sort": "added"},
                                    headers=self._ov())
            r.raise_for_status()
            page = r.json()
            for q in page["results"]:
                m, by = q.get("media") or {}, q.get("requestedBy") or {}
                out.append({"id": q["id"], "type": q.get("type"), "status": q.get("status"),
                            "at": q.get("createdAt"), "plex_id": by.get("plexId"),
                            "by": by.get("displayName") or by.get("plexUsername"),
                            "tmdb": m.get("tmdbId"), "tvdb": m.get("tvdbId"), "rating_key": m.get("ratingKey"),
                            "seasons": [s.get("seasonNumber") for s in q.get("seasons") or []]})
            skip += 100
            if skip >= page["pageInfo"]["results"]:
                return out

    async def arr_instances(self):
        """Radarr/Sonarr connection details: configured directly, else discovered from
        Overseerr/Jellyseerr (its non-4K default server)."""
        out = {}
        for app, url, key in (("radarr", C.RADARR_URL, C.RADARR_API_KEY), ("sonarr", C.SONARR_URL, C.SONARR_API_KEY)):
            if url and key:
                base = url if url.endswith("/api/v3") else url + "/api/v3"
                out[app] = {"base": base, "key": key, "name": app.capitalize(), "root": None}
        for app in ("radarr", "sonarr"):
            if app in out or not (C.SEERR_URL and C.SEERR_API_KEY):
                continue
            r = await self.http.get(f"{C.SEERR_URL}/api/v1/settings/{app}", headers=self._ov())
            r.raise_for_status()
            rows = [x for x in r.json() if not x.get("is4k")] or r.json()
            x = next((y for y in rows if y.get("isDefault")), rows[0]) if rows else None
            if x:
                scheme = "https" if x.get("useSsl") else "http"
                out[app] = {"base": f"{scheme}://{x['hostname']}:{x['port']}{x.get('baseUrl') or ''}/api/v3",
                            "key": x["apiKey"], "name": x.get("name"), "root": x.get("activeDirectory")}
        return out

    # ----------------------------------------------------------------- arr
    async def arr(self, inst, method, path, **kw):
        r = await self.http.request(method, inst["base"] + path, headers={"X-Api-Key": inst["key"]}, **kw)
        r.raise_for_status()
        return r.json() if r.content else None

    async def radarr_movies(self, inst):
        rows = await self.arr(inst, "GET", "/movie")
        return [{"id": m["id"], "title": m.get("title"), "year": m.get("year"), "tmdb": m.get("tmdbId"),
                 "path": m.get("path"), "monitored": m.get("monitored"), "has_file": m.get("hasFile"),
                 "profile": m.get("qualityProfileId"), "size": m.get("sizeOnDisk"),
                 "file": ((m.get("movieFile") or {}).get("relativePath")), "added": m.get("added")} for m in rows]

    async def sonarr_series(self, inst):
        rows = await self.arr(inst, "GET", "/series")
        return [{"id": s["id"], "title": s.get("title"), "tvdb": s.get("tvdbId"), "path": s.get("path"),
                 "monitored": s.get("monitored"), "profile": s.get("qualityProfileId"), "status": s.get("status"),
                 "seasons": {str(x["seasonNumber"]): x.get("monitored") for x in s.get("seasons") or []},
                 "added": s.get("added")} for s in rows]

    async def arr_profiles(self, inst):
        return {p["id"]: p["name"] for p in await self.arr(inst, "GET", "/qualityprofile")}

    async def arr_media_mgmt(self, inst):
        return await self.arr(inst, "GET", "/config/mediamanagement")

    # ------------------------------------------------------------ capacity
    async def capacity(self):
        if C.UNRAID_URL and C.UNRAID_API_KEY:
            return await self.unraid_array()
        if C.CAPACITY_PATHS:
            return await asyncio.to_thread(disk_capacity, C.CAPACITY_PATHS)
        return None

    async def unraid_array(self):
        q = "{ array { state capacity { kilobytes { free used total } } disks { name fsSize fsFree fsUsed status } } }"
        r = await self.http.post(C.UNRAID_URL, headers={"x-api-key": C.UNRAID_API_KEY, "Content-Type": "application/json"},
                                 content=json.dumps({"query": q}))
        r.raise_for_status()
        d = r.json()
        if d.get("errors"):
            raise RuntimeError(d["errors"][0].get("message"))
        a = d["data"]["array"]
        kb = a["capacity"]["kilobytes"]
        # Unraid's "kilobytes" are decimal: the disks' fsSize values sum exactly to
        # capacity.total and exceed the drives' KiB sizes, so bytes = value * 1000.
        k = 1000
        return {"state": a["state"], "total": int(kb["total"]) * k, "used": int(kb["used"]) * k,
                "free": int(kb["free"]) * k,
                "disks": [{"name": x["name"], "size": (x["fsSize"] or 0) * k, "used": (x["fsUsed"] or 0) * k,
                           "free": (x["fsFree"] or 0) * k, "status": x["status"]} for x in a["disks"]]}


def disk_capacity(paths):
    """Free space of the filesystem(s) holding the media; each device counted once."""
    seen, disks = set(), []
    for p in paths:
        st = os.stat(p)
        if st.st_dev in seen:
            continue
        seen.add(st.st_dev)
        u = shutil.disk_usage(p)
        disks.append({"name": p, "size": u.total, "used": u.used, "free": u.free, "status": "ok"})
    return {"state": "disk", "total": sum(d["size"] for d in disks), "used": sum(d["used"] for d in disks),
            "free": sum(d["free"] for d in disks), "disks": disks}


def walk_local(pairs):
    """Walk media mounted into this container; report paths in Plex's own form so they join."""
    files, errors, t0 = [], [], time.time()
    for plex_root, local_root in pairs:
        for dirpath, _dirs, names in os.walk(local_root, onerror=lambda e: errors.append(str(e))):
            for n in names:
                full = os.path.join(dirpath, n)
                try:
                    size = os.lstat(full).st_size
                except OSError as ex:
                    errors.append(str(ex))
                    continue
                files.append([plex_root + full[len(local_root):].replace(os.sep, "/"), size])
    return {"at": time.time(), "roots": [p for p, _ in pairs], "files": files, "errors": errors[:50],
            "seconds": round(time.time() - t0)}


async def gather_all(src: Sources, progress=lambda msg: None):
    """Fetch every upstream concurrently. Optional sources degrade to None with a note."""
    notes = []

    async def opt(name, coro):
        try:
            v = await coro
            progress(f"{name} ✓")
            return v
        except Exception as ex:  # an optional source must never sink the rebuild
            log.warning("%s failed: %s", name, ex)
            notes.append(f"{name}: {ex}")
            progress(f"{name} failed")
            return None

    sections = await src.plex_sections()
    server = await src.plex_server()
    msecs = [s for s in sections if s["type"] == "movie"]
    ssecs = [s for s in sections if s["type"] == "show"]

    async def movies():
        out = []
        for s in msecs:
            out += [dict(m, section=s["id"]) for m in await src.plex_movies(s["id"])]
        progress(f"plex movies ✓ {len(out)}")
        return out

    async def shows():
        sh, se, ep = [], [], []
        for s in ssecs:
            sh += [dict(x, section=s["id"]) for x in await src.plex_shows(s["id"])]
            se += await src.plex_seasons(s["id"])
            ep += await src.plex_episodes(s["id"])
        progress(f"plex episodes ✓ {len(ep)}")
        return sh, se, ep

    async def arrs():
        inst = await src.arr_instances()
        out = {"instances": {k: {"base": v["base"], "name": v["name"]} for k, v in inst.items()}}
        if "radarr" in inst:
            out["radarr"], out["radarr_profiles"], out["radarr_mm"] = await asyncio.gather(
                src.radarr_movies(inst["radarr"]), src.arr_profiles(inst["radarr"]), src.arr_media_mgmt(inst["radarr"]))
        if "sonarr" in inst:
            out["sonarr"], out["sonarr_profiles"], out["sonarr_mm"] = await asyncio.gather(
                src.sonarr_series(inst["sonarr"]), src.arr_profiles(inst["sonarr"]), src.arr_media_mgmt(inst["sonarr"]))
        progress("radarr/sonarr ✓")
        return out

    async def none():
        return None

    has_tautulli = bool(C.TAUTULLI_URL and C.TAUTULLI_API_KEY)
    has_seerr = bool(C.SEERR_URL and C.SEERR_API_KEY)
    has_arr = has_seerr or bool(C.RADARR_URL and C.RADARR_API_KEY) or bool(C.SONARR_URL and C.SONARR_API_KEY)
    progress("fetching")
    (mv, (sh, se, ep), phist, thist, tusers, reqs, arr, array) = await asyncio.gather(
        movies(), shows(),
        opt("plex history", src.plex_history()),
        opt("tautulli history", src.tautulli_history()) if has_tautulli else none(),
        opt("tautulli users", src.tautulli_users()) if has_tautulli else opt("plex accounts", src.plex_users()),
        opt("requests", src.seerr_requests()) if has_seerr else none(),
        opt("radarr/sonarr", arrs()) if has_arr else none(),
        opt("capacity", src.capacity()),
    )
    return {"server": server, "sections": sections, "movies": mv, "shows": sh, "seasons": se, "episodes": ep,
            "plex_history": phist or [], "tautulli_history": thist or [], "users": tusers or [],
            "requests": reqs or [], "arr": arr or {}, "array": array, "notes": notes}
