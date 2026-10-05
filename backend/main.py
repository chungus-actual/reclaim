"""reclaim — see what your Plex library costs on disk, and reclaim it.

Run: uvicorn main:app --host 0.0.0.0 --port 8892
"""
import asyncio
import base64
import binascii
import datetime as dt
import gzip
import hashlib
import json
import logging
import secrets
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import actions
import config as C
import downgrade as D
import settings
import store
from model import CATEGORIES, Model
from sources import Sources, gather_all, walk_local

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("reclaim")
logging.getLogger("httpx").setLevel(logging.WARNING)

RAW_FILE = C.DATA_DIR / "raw.json.gz"
WALK_FILE = C.DATA_DIR / "walk.json.gz"
THUMBS = C.DATA_DIR / "thumbs"
THUMBS.mkdir(exist_ok=True)

app = FastAPI(title="reclaim")
app.add_middleware(GZipMiddleware, minimum_size=2000)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    """Optional HTTP basic auth (RECLAIM_USER + RECLAIM_PASSWORD). /api/health stays open."""
    if C.auth_on() and request.url.path != "/api/health":
        ok = False
        h = request.headers.get("authorization", "")
        if h.lower().startswith("basic "):
            try:
                user, _, pw = base64.b64decode(h[6:]).decode("utf-8").partition(":")
                ok = secrets.compare_digest(user, C.AUTH_USER) and C.check_password(pw)
            except (binascii.Error, UnicodeDecodeError):
                ok = False
        if not ok:
            return Response("authentication required", status_code=401,
                            headers={"WWW-Authenticate": 'Basic realm="reclaim"'})
    return await call_next(request)


class State:
    raw = None
    walk = None
    model = None
    client_cache = None    # (generated, bytes) of the serialized model
    src = None
    status = {"refreshing": False, "phase": None, "started": None, "last_ok": None, "last_error": None}
    job = None             # running/finished delete job
    lock = asyncio.Lock()
    searches = {}          # downgrade search jobs (in memory; the arr's release cache lives 30 min anyway)
    dg_lock = asyncio.Lock()
    refresh_due = None
    nightly_task = None


S = State()


def _save_gz(path, obj):
    tmp = path.with_suffix(".tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    tmp.replace(path)


def _load_gz(path):
    if not path.exists():
        return None
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return json.load(f)


def rebuild():
    S.model = Model(S.raw, S.walk, store.keep_keys())
    S.client_cache = None
    log.info("model built: %d titles, %d plays in %d ms", len(S.model.titles), len(S.model.plays), S.model.build_ms)


def guard(request: Request):
    """Writes need a custom header: browsers can't send it cross-origin without a
    CORS preflight this app never grants, so another site can't POST here."""
    if request.headers.get("x-reclaim") != "1":
        raise HTTPException(403, "missing X-Reclaim header")


def destructive(request: Request):
    guard(request)
    if C.READ_ONLY:
        raise HTTPException(403, "reclaim is in read-only mode (RECLAIM_READ_ONLY)")


# ------------------------------------------------------------------ refresh
async def refresh(reason="manual"):
    if C.DEMO or S.status["refreshing"] or C.needs_setup():
        return
    S.status.update(refreshing=True, started=time.time(), phase="starting", last_error=None)
    try:
        raw = await gather_all(S.src, progress=lambda m: S.status.update(phase=m))
        raw["fetched_at"] = time.time()
        S.status["phase"] = "building model"
        async with S.lock:
            S.raw = raw
            await asyncio.to_thread(rebuild)
        await asyncio.to_thread(_save_gz, RAW_FILE, raw)
        store.record_capacity(raw.get("array"), sum(S.model.lib_bytes.values()), reason)
        if C.WALK_PATHS and C.WALK_AFTER_REFRESH:
            await walk_now()
        S.status.update(last_ok=time.time(), phase=None)
    except Exception as ex:
        log.exception("refresh failed")
        S.status.update(last_error=f"{type(ex).__name__}: {ex}", phase=None)
    finally:
        S.status["refreshing"] = False


async def nightly():
    while True:
        now = dt.datetime.now()
        nxt = now.replace(hour=C.REFRESH_HOUR, minute=0, second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        await asyncio.sleep((nxt - now).total_seconds())
        await refresh("nightly")


async def walk_now():
    """Walk the media mounted at WALK_PATHS and rebuild with the result."""
    S.status["phase"] = "walking media folders"
    walk = await asyncio.to_thread(walk_local, C.WALK_PATHS)
    if not walk["files"] and walk["errors"]:
        log.warning("walk found nothing: %s", walk["errors"][:3])
        return
    S.walk = walk
    await asyncio.to_thread(_save_gz, WALK_FILE, walk)
    if S.raw:
        async with S.lock:
            await asyncio.to_thread(rebuild)
    log.info("walked %d files in %ss", len(walk["files"]), walk["seconds"])


@app.on_event("startup")
async def startup():
    store.init()
    cfg = C.summary()
    log.info("config: %s", cfg)
    if C.DEMO:
        S.src = Sources()
        S.walk, S.raw = _load_gz(WALK_FILE), _load_gz(RAW_FILE)
        if S.raw:
            await asyncio.to_thread(rebuild)
            log.info("demo mode: made-up library from %s, read-only, no services contacted", C.DATA_DIR)
        else:
            log.warning("demo mode but no demo data: run  python tools/demo_data.py --data %s", C.DATA_DIR)
        return
    if C.needs_setup():
        log.info("not set up yet: open the page and connect Plex in Settings")
    elif not C.PLEX_TOKEN:
        log.warning("no Plex token: only works if this host is in Plex's allowedNetworks, "
                    "and watch history needs the owner's token")
    S.src = Sources()
    S.walk = _load_gz(WALK_FILE)
    S.raw = _load_gz(RAW_FILE)
    if S.raw:
        await asyncio.to_thread(rebuild)
    age_h = (time.time() - (S.raw or {}).get("fetched_at", 0)) / 3600
    if not S.raw or age_h > C.STALE_HOURS:
        asyncio.create_task(refresh("startup"))
    S.nightly_task = asyncio.create_task(nightly())
    asyncio.create_task(dg_poller())


@app.on_event("shutdown")
async def shutdown():
    await S.src.close()


def need_model():
    if S.model is None:
        raise HTTPException(503, "connect Plex in Settings first" if C.needs_setup() else "first build in progress")
    return S.model


# ---------------------------------------------------------------------- api
@app.get("/api/health")
async def health():
    return {"ok": True, "model": S.model is not None}


@app.get("/api/config")
async def api_config():
    return {**C.summary(), "display_paths": C.DISPLAY_PATHS, "walk_roots": [p for p, _ in C.WALK_PATHS]}


# ----------------------------------------------------------------- settings
SOURCE_KEYS = ("PLEX_URL", "PLEX_TOKEN", "TAUTULLI_URL", "TAUTULLI_API_KEY", "RADARR_URL", "RADARR_API_KEY",
               "SONARR_URL", "SONARR_API_KEY", "SEERR_URL", "SEERR_API_KEY", "UNRAID_URL", "UNRAID_API_KEY",
               "CAPACITY_PATHS", "WALK_PATHS")


def _settings_payload():
    p = settings.payload()
    p["plex_roots"] = sorted({r.rstrip("/") for sec in S.model.sections.values() for r in sec["roots"]}) if S.model else []
    return p


@app.get("/api/settings")
async def api_settings():
    return _settings_payload()


@app.post("/api/settings")
async def api_settings_save(request: Request):
    guard(request)
    if C.DEMO:
        raise HTTPException(403, "this is the demo: settings can't be changed")
    body = await request.json()
    before = C.effective()
    res = settings.save(body.get("values") or {}, body.get("clear") or [])
    if not res["ok"]:
        return JSONResponse({"ok": False, "errors": res["errors"]}, status_code=400)
    after = C.effective()
    changed = [k for k in SOURCE_KEYS if before.get(k) != after.get(k)]
    if before.get("RECLAIM_REFRESH_HOUR") != after.get("RECLAIM_REFRESH_HOUR") and S.nightly_task:
        S.nightly_task.cancel()
        S.nightly_task = asyncio.create_task(nightly())
    if changed and not C.needs_setup():
        S.client_cache = None
        asyncio.create_task(refresh("settings changed"))
    log.info("settings saved; changed: %s", changed or "none that need a rebuild")
    return {"ok": True, "rebuilding": bool(changed) and not C.needs_setup(), **_settings_payload()}


@app.post("/api/settings/test")
async def api_settings_test(request: Request):
    guard(request)
    body = await request.json()
    return await settings.test(body.get("group"), body.get("values") or {}, S.model)


@app.post("/api/settings/plex/pin")
async def api_plex_pin(request: Request):
    guard(request)
    try:
        return await settings.plex_pin()
    except Exception as ex:
        raise HTTPException(502, f"couldn't reach plex.tv: {ex}")


@app.post("/api/settings/plex/pin/{pin_id}")
async def api_plex_pin_check(pin_id: int, request: Request):
    guard(request)
    try:
        return await settings.plex_pin_check(pin_id)
    except Exception as ex:
        raise HTTPException(502, f"plex.tv: {ex}")


@app.get("/api/settings/browse")
async def api_browse(path: str = "/"):
    return settings.browse(path)


@app.post("/api/walk/run")
async def api_walk_run(request: Request):
    guard(request)
    if not C.WALK_PATHS:
        raise HTTPException(400, "set up Disk walk in Settings first")
    asyncio.create_task(walk_now())
    return {"started": True}


@app.get("/api/status")
async def status():
    m = S.model
    return {**S.status, "generated": m.generated if m else None, "job": S.job,
            "walk_at": m.walk_at if m else None}


@app.post("/api/refresh")
async def api_refresh(request: Request):
    guard(request)
    asyncio.create_task(refresh("manual"))
    return {"started": True}


@app.get("/api/model")
async def api_model():
    m = need_model()
    if not S.client_cache or S.client_cache[0] is not m:
        S.client_cache = (m, json.dumps(m.client(), ensure_ascii=False, separators=(",", ":")).encode())
    return Response(S.client_cache[1], media_type="application/json")


@app.get("/api/title/{key}")
async def api_title(key: str):
    d = need_model().detail(key)
    if not d:
        raise HTTPException(404, "not in the library")
    return d


@app.get("/api/capacity")
async def api_capacity():
    return store.capacity_history()


@app.get("/api/unindexed")
async def api_unindexed():
    m = need_model()
    titles = m.titles
    return {"walk_at": m.walk_at, "missing_on_disk": m.missing_on_disk,
            "categories": {c: {"label": CATEGORIES[c], "files": v[0], "bytes": v[1]} for c, v in m.unindexed_by_cat.items()},
            "groups": [dict(g, name=(titles[g["title"]]["title"] if g["title"] in titles else None))
                       for g in m.unindexed]}


@app.post("/api/walk")
async def api_walk(request: Request):
    """Disk walk posted from another machine (tools/remote_walk.py) when the media isn't mounted here."""
    guard(request)
    body = await request.body()
    if request.headers.get("content-encoding") == "gzip":
        body = gzip.decompress(body)
    walk = json.loads(body)
    if not isinstance(walk.get("files"), list):
        raise HTTPException(400, "files[] required")
    walk["at"] = walk.get("at") or time.time()
    S.walk = walk
    await asyncio.to_thread(_save_gz, WALK_FILE, walk)
    if S.raw:
        async with S.lock:
            await asyncio.to_thread(rebuild)
    return {"files": len(walk["files"]), "unindexed_groups": len(S.model.unindexed) if S.model else None}


def _demo_poster(key: str) -> str:
    """A plain poster for made-up titles: the title on a colour taken from its key."""
    from xml.sax.saxutils import escape
    m = S.model
    t = (m.titles.get(key) if m else None) or (m.titles.get(m.seasons[key]["show"]) if m and key in m.seasons else None) or {}
    words, lines = str(t.get("title") or "Untitled").split(), [""]
    for w in words:
        if len(lines[-1]) + len(w) > 11 and lines[-1]:
            lines.append("")
        lines[-1] = (lines[-1] + " " + w).strip()
    hue = int(hashlib.sha1(key.encode()).hexdigest()[:4], 16) % 360
    y0 = 180 - (len(lines) - 1) * 17
    text = "".join(f'<text x="120" y="{y0 + i * 34}" text-anchor="middle">{escape(l)}</text>' for i, l in enumerate(lines[:4]))
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 360" width="240" height="360">'
            f'<defs><linearGradient id="g" x1="0" y1="0" x2="0.4" y2="1"><stop offset="0" stop-color="hsl({hue},42%,40%)"/>'
            f'<stop offset="1" stop-color="hsl({(hue + 35) % 360},48%,14%)"/></linearGradient></defs>'
            f'<rect width="240" height="360" fill="url(#g)"/><circle cx="196" cy="62" r="88" fill="hsla({(hue + 180) % 360},60%,70%,.12)"/>'
            f'<g font-family="Georgia,serif" font-size="27" fill="#f4efe6">{text}</g>'
            f'<text x="120" y="318" text-anchor="middle" font-family="Helvetica,Arial,sans-serif" font-size="15" '
            f'letter-spacing="3" fill="#f4efe6" opacity=".7">{escape(str(t.get("year") or ""))}</text></svg>')


@app.get("/api/thumb")
async def api_thumb(path: str, w: int = 240, h: int = 360):
    if not path.startswith("/library/metadata/"):
        raise HTTPException(400, "bad path")
    if C.DEMO:
        return Response(_demo_poster(path.split("/")[3]), media_type="image/svg+xml",
                        headers={"Cache-Control": "public, max-age=604800"})
    f = THUMBS / (hashlib.sha1(f"{path}|{w}x{h}".encode()).hexdigest() + ".jpg")
    if not f.exists():
        try:
            data, _ = await S.src.plex_thumb(path, w, h)
        except Exception:
            raise HTTPException(404, "no art")
        f.write_bytes(data)
    return FileResponse(f, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})


# ----------------------------------------------------------- shortlist/keep
def _resolve(m, it):
    """Turn a page request into a shortlist row with authoritative bytes/label."""
    kind, key = it.get("kind"), str(it.get("key"))
    if kind in ("movie", "show"):
        t = m.titles.get(key)
        if not t or t["kind"] != kind:
            return None
        yr = f" ({t['year']})" if t["year"] else ""
        return {"kind": kind, "key": key, "title_key": key, "label": f"{t['title']}{yr}", "bytes": t["size"]}
    if kind == "season":
        s = m.seasons.get(key)
        if not s or s["show"] not in m.titles:
            return None
        return {"kind": "season", "key": key, "title_key": s["show"],
                "label": f"{m.titles[s['show']]['title']} · {s['title']}", "bytes": s["size"]}
    if kind == "version":
        t = m.titles.get(str(it.get("title_key")))
        v = next((v for v in (t or {}).get("versions", []) if v["id"] == it.get("media_id")), None)
        if not v or len(t["versions"]) < 2:
            return None
        return {"kind": "version", "key": f"{t['key']}:{v['id']}", "title_key": t["key"], "media_id": v["id"],
                "label": f"{t['title']} · {v['res'] or '?'} {v['vcodec'] or ''} version".strip(), "bytes": v["bytes"]}
    return None


@app.get("/api/shortlist")
async def api_shortlist():
    return store.shortlist()


@app.post("/api/shortlist")
async def api_shortlist_add(request: Request):
    guard(request)
    m = need_model()
    body = await request.json()
    keep = store.keep_keys()
    rows, skipped = [], []
    for it in body.get("items", []):
        r = _resolve(m, it)
        if not r:
            skipped.append(it)
        elif r["title_key"] in keep:
            skipped.append(dict(it, why="kept"))
        else:
            rows.append(r)
    store.shortlist_add(rows)
    return {"added": len(rows), "skipped": skipped, "shortlist": store.shortlist()}


@app.post("/api/shortlist/remove")
async def api_shortlist_remove(request: Request):
    guard(request)
    body = await request.json()
    if body.get("all"):
        store.shortlist_clear()
    else:
        store.shortlist_remove([int(i) for i in body.get("ids", [])])
    return store.shortlist()


@app.post("/api/keep")
async def api_keep(request: Request):
    guard(request)
    body = await request.json()
    key, on = str(body["key"]), bool(body.get("on", True))
    store.keep_set(key, on)
    if S.model:
        (S.model.keep.add if on else S.model.keep.discard)(key)
        S.client_cache = None
    return {"key": key, "kept": on, "shortlist": store.shortlist()}


# ------------------------------------------------------------------- delete
@app.post("/api/delete")
async def api_delete(request: Request):
    destructive(request)
    m = need_model()
    body = await request.json()
    if body.get("confirm") != "DELETE":
        raise HTTPException(400, "type DELETE to confirm")
    if S.job and S.job.get("running"):
        raise HTTPException(409, "a delete is already running")
    if S.status["refreshing"]:
        raise HTTPException(409, "wait for the refresh to finish")
    ids = set(int(i) for i in body.get("ids", []))
    items = [r for r in store.shortlist() if r["id"] in ids]
    if not items:
        raise HTTPException(400, "nothing selected")
    for it in items:
        if it["kind"] == "season":
            it["season_index"] = (m.seasons.get(it["key"]) or {}).get("index")
    S.job = {"running": True, "started": time.time(), "total": len(items), "done": 0, "phase": None,
             "results": [], "unmonitor": bool(body.get("unmonitor", True)), "freed": 0}
    asyncio.create_task(_delete_job(items, S.job["unmonitor"]))
    return S.job


async def _delete_job(items, unmon):
    job = S.job
    try:
        async with S.lock:
            gone = set()
            async for it, res, meta in actions.run(S.src, S.model, S.raw, items, unmon,
                                                   lambda p: job.update(phase=p)):
                if res["ok"]:
                    gone.update(meta["files"])
                    # a pending downgrade would re-import a file into what was just deleted
                    for j in store.downgrades("title_key=? AND state='grabbed'", it["title_key"]):
                        if it["kind"] != "season" or j["season"] == it.get("season_index"):
                            await _dg_cancel(j, "title deleted")
                store.log_deletion({**it, "ok": res["ok"], "plex": res.get("plex") or res.get("error"),
                                    "arr": res.get("arr"), "meta": {**meta, "files": meta["files"][:400]}})
                if res["ok"]:
                    store.shortlist_remove([it["id"]])
                    job["freed"] += it["bytes"]
                job["results"].append(res)
                job["done"] += 1
            if gone and S.walk:
                # the stored walk predates the delete: without this its files would
                # resurface as "not in Plex" until the next walk
                S.walk["files"] = [f for f in S.walk["files"] if f[0] not in gone]
                await asyncio.to_thread(_save_gz, WALK_FILE, S.walk)
            job["phase"] = "rebuilding"
            try:
                S.raw["array"] = await S.src.capacity()
            except Exception as ex:
                log.warning("array re-read failed: %s", ex)
            await asyncio.to_thread(rebuild)
        await asyncio.to_thread(_save_gz, RAW_FILE, S.raw)
        store.record_capacity(S.raw.get("array"), sum(S.model.lib_bytes.values()), "after delete")
    except Exception as ex:
        log.exception("delete job failed")
        job["error"] = f"{type(ex).__name__}: {ex}"
    finally:
        job["running"] = False
        job["finished"] = time.time()
        job["phase"] = None


@app.get("/api/log")
async def api_log():
    return store.deletions()


# ---------------------------------------------------------------- downgrade
def _runtime_min(m, t, season_key=None):
    if t["kind"] == "movie":
        return (t["duration"] or 0) / 60000 or None
    return sum((e[5] or 0) for e in m.episodes.values() if e[1] == season_key) / 60000 or None


@app.post("/api/downgrade/search")
async def dg_search(request: Request):
    """Interactive search for smaller releases. Movies take seconds; a TV season can take minutes."""
    destructive(request)
    m = need_model()
    body = await request.json()
    key, target = str(body["key"]), int(body["target"])
    t = m.titles.get(key)
    if not t:
        raise HTTPException(404, "not in the library")
    if not t.get("arr"):
        raise HTTPException(400, f"not in {'Radarr' if t['kind'] == 'movie' else 'Sonarr'}")
    if target not in D.TIERS:
        raise HTTPException(400, f"target must be one of {D.TIERS}")
    if t["kind"] == "movie":
        parts = [{"season": None, "season_key": None, "label": t["title"]}]
    else:
        wanted = body.get("seasons")
        seasons = sorted((s for s in m.seasons.values() if s["show"] == key and s["size"] > 0
                          and (s["key"] in wanted if wanted else (s["index"] or 0) > 0)),
                         key=lambda s: s["index"] or 0)
        parts = [{"season": s["index"], "season_key": s["key"], "label": s["title"], "plex_size": s["size"]}
                 for s in seasons]
        if not parts:
            raise HTTPException(400, "no seasons with files")
    for p in parts:
        p["status"] = "pending"
    job = {"id": uuid.uuid4().hex[:10], "key": key, "kind": t["kind"], "app": t["arr"]["app"],
           "item_id": t["arr"]["id"], "target": target, "title": t["title"], "started": time.time(),
           "status": "running", "parts": parts}
    for k in [k for k, v in S.searches.items() if time.time() - v["started"] > 7200]:
        del S.searches[k]
    S.searches[job["id"]] = job
    asyncio.create_task(_dg_run_search(job, t))
    return job


async def _dg_run_search(job, t):
    try:
        inst = (await S.src.arr_instances())[job["app"]]
        for part in job["parts"]:
            part["status"] = "searching"
            part["at"] = time.time()
            try:
                if job["kind"] == "movie":
                    ctx = await D.movie_context(S.src, inst, job["item_id"])
                    rels = await S.src.arr(inst, "GET", "/release", params={"movieId": job["item_id"]})
                else:
                    ctx = await D.season_context(S.src, inst, job["item_id"], part["season"])
                    rels = await S.src.arr(inst, "GET", "/release",
                                           params={"seriesId": job["item_id"], "seasonNumber": part["season"]})
                rt = _runtime_min(S.model, t, part["season_key"])
                part.update(ctx=ctx, runtime_min=round(rt) if rt else None, total=len(rels),
                            candidates=D.candidates(rels, job["target"], ctx["file_size"], rt, part["season"],
                                                    "Radarr" if job["app"] == "radarr" else "Sonarr",
                                                    prefer_multi=ctx.get("series_type") == "anime"),
                            status="done", searched=time.time())
            except Exception as ex:
                log.exception("downgrade search failed")
                part.update(status="error", error=f"{type(ex).__name__}: {ex}")
    finally:
        job["status"] = "done"


@app.get("/api/downgrade/search/{sid}")
async def dg_search_get(sid: str):
    job = S.searches.get(sid)
    if not job:
        raise HTTPException(404, "search expired — run it again")
    return job


async def _dg_profile_state(inst, app_, item):
    """(current profile id, the profile it had before reclaim ever touched it)."""
    path = f"/movie/{item}" if app_ == "radarr" else f"/series/{item}"
    cur = (await S.src.arr(inst, "GET", path))["qualityProfileId"]
    names = {p["id"]: p["name"] for p in await S.src.arr(inst, "GET", "/qualityprofile")}
    if names.get(cur, "").startswith("Reclaim ↓"):
        prior = store.downgrades("app=? AND item_id=? AND old_profile IS NOT NULL", app_, item)
        orig = prior[-1]["old_profile"] if prior else None      # earliest job remembers the real original
    else:
        orig = cur
    return cur, orig


@app.post("/api/downgrade/grab")
async def dg_grab(request: Request):
    destructive(request)
    m = need_model()
    body = await request.json()
    job = S.searches.get(body.get("search_id"))
    if not job:
        raise HTTPException(404, "search expired — run it again")
    t = m.titles.get(job["key"])
    if not t:
        raise HTTPException(404, "not in the library any more")
    app_, item = job["app"], job["item_id"]
    async with S.dg_lock:
        active = store.downgrades("app=? AND item_id=? AND state='grabbed'", app_, item)
        if any(a["target"] != job["target"] for a in active):
            raise HTTPException(409, f"a ↓{active[0]['target']}p downgrade is still running for this title — "
                                     "let it finish or cancel it first")
        inst = (await S.src.arr_instances())[app_]
        pid = await D.ensure_profile(S.src, inst, job["target"])
        cur, orig = await _dg_profile_state(inst, app_, item)
        if cur != pid:
            await D.set_profile(S.src, inst, app_, item, pid)
        results = []
        for pick in body.get("picks", []):
            part = job["parts"][int(pick["part"])]
            cand = next((c for c in part.get("candidates") or [] if c["guid"] == pick["guid"]), None)
            label = t["title"] + (f" · {part['label']}" if part["season"] is not None else "")
            if not cand:
                results.append({"label": label, "ok": False, "error": "release not in this search"})
                continue
            if time.time() - part.get("searched", 0) > 25 * 60:
                results.append({"label": label, "ok": False,
                                "error": "search is over 25 min old (the arr forgets results at 30) — search again"})
                continue
            if any(a["season"] == part["season"] for a in active):
                results.append({"label": label, "ok": False, "error": "already downgrading"})
                continue
            try:
                await D.grab(S.src, inst, cand)
            except Exception as ex:
                results.append({"label": label, "ok": False, "error": f"grab refused: {ex}"})
                continue
            ctx = part["ctx"]
            jid = store.dg_add({
                "app": app_, "item_id": item, "title_key": t["key"], "season": part["season"], "label": label,
                "target": job["target"], "release": cand["title"], "quality": cand["quality"],
                "indexer": cand["indexer"], "new_size": cand["size"], "old_size": ctx["file_size"],
                "old_quality": ctx["file_quality"], "old_profile": orig, "old_file_id": ctx.get("file_id"),
                "old_file_ids": ",".join(map(str, ctx.get("file_ids") or [])), "state": "grabbed",
                "note": "sent to the download client", "folder": t["folder"], "section": t["section"]})
            results.append({"label": label, "ok": True, "id": jid, "saves": ctx["file_size"] - cand["size"]})
        if not any(r["ok"] for r in results) and cur != pid and not store.downgrades(
                "app=? AND item_id=? AND state IN ('grabbed','imported')", app_, item):
            await D.set_profile(S.src, inst, app_, item, cur)
    asyncio.create_task(_dg_tick_soon(90))
    return {"results": results, "profile": D.profile_name(job["target"])}


async def _dg_revert_if_alone(inst, j):
    others = store.downgrades("app=? AND item_id=? AND id<>? AND state IN ('grabbed','imported')",
                              j["app"], j["item_id"], j["id"])
    if not others and j["old_profile"]:
        try:
            await D.set_profile(S.src, inst, j["app"], j["item_id"], j["old_profile"])
            return True
        except Exception as ex:
            log.warning("profile revert failed for %s: %s", j["label"], ex)
    return False


async def _dg_cancel(j, why):
    inst = (await S.src.arr_instances())[j["app"]]
    params = {"movieId": j["item_id"]} if j["app"] == "radarr" else {"seriesId": j["item_id"]}
    removed = 0
    for q in await S.src.arr(inst, "GET", "/queue/details", params=params) or []:
        season = (q.get("episode") or {}).get("seasonNumber", q.get("seasonNumber"))
        if j["app"] == "radarr" or season in (None, j["season"]):
            await S.src.arr(inst, "DELETE", f"/queue/{q['id']}",
                            params={"removeFromClient": "true", "blocklist": "false"})
            removed += 1
    store.dg_update(j["id"], state="cancelled", note=f"{why} · removed {removed} from the queue")
    await _dg_revert_if_alone(inst, j)


@app.post("/api/downgrade/cancel")
async def dg_cancel(request: Request):
    destructive(request)
    body = await request.json()
    rows = store.downgrades("id=?", int(body["id"]))
    if not rows or rows[0]["state"] != "grabbed":
        raise HTTPException(400, "only a running downgrade can be cancelled")
    async with S.dg_lock:
        await _dg_cancel(rows[0], "cancelled by you")
    return store.downgrades("id=?", rows[0]["id"])[0]


@app.get("/api/downgrades")
async def dg_list():
    return store.downgrades()


async def _dg_tick_soon(delay):
    await asyncio.sleep(delay)
    try:
        await dg_tick()
    except Exception:
        log.exception("downgrade poll failed")


async def dg_tick():
    async with S.dg_lock:
        jobs = store.downgrades("state='grabbed'")
        if not jobs:
            return
        inst_all = await S.src.arr_instances()
        imported = False
        for j in jobs:
            inst = inst_all[j["app"]]
            try:
                state, note, size = await D.check(S.src, inst, j)
            except Exception as ex:
                log.warning("downgrade check %s: %s", j["label"], ex)
                continue
            if state == "imported":
                store.dg_update(j["id"], state="imported", note=note, final_size=size)
                imported = True
                if j["folder"] and j["section"]:
                    try:
                        await S.src.plex_scan(j["section"], j["folder"])
                    except Exception as ex:
                        log.warning("plex scan %s: %s", j["folder"], ex)
            elif state == "failed":
                reverted = await _dg_revert_if_alone(inst, j)
                store.dg_update(j["id"], state="failed",
                                note=note + (" · original profile restored" if reverted else ""))
            elif note != j["note"]:
                store.dg_update(j["id"], note=note)
    if imported and not S.refresh_due:
        # give Plex a few minutes to scan the replaced files, then rebuild sizes
        S.refresh_due = time.time() + 300

        async def later():
            await asyncio.sleep(300)
            S.refresh_due = None
            await refresh("after downgrade")
        asyncio.create_task(later())


async def dg_poller():
    while True:
        await asyncio.sleep(120)
        try:
            await dg_tick()
        except Exception:
            log.exception("downgrade poll failed")


# ------------------------------------------------------------------- static
app.mount("/static", StaticFiles(directory=C.APP_DIR / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(C.APP_DIR / "static" / "index.html", headers={"Cache-Control": "no-cache"})
