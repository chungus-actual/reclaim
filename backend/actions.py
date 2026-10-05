"""Deletion: Plex removes the files; the *arr keeps the record, unmonitored.

Radarr/Sonarr ship with "unmonitor deleted movies/episodes" switched off. A file deleted
outside them leaves the item "missing + monitored", so the next matching release on RSS gets
grabbed again. Unmonitoring just the deleted item (not a global setting change) gives
"missing, recorded, never re-grabbed".
"""
import logging

log = logging.getLogger("reclaim.actions")


def plex_path(it):
    if it["kind"] == "version":
        return f"/library/metadata/{it['title_key']}/media/{it['media_id']}"
    return f"/library/metadata/{it['key']}"


async def unmonitor(src, inst, it, title):
    arr = title.get("arr")
    if not arr:
        return "no radarr/sonarr match"
    app = arr["app"]
    if app not in inst:
        return f"{app} not configured"
    i = inst[app]
    if it["kind"] == "version":
        return "version delete: arr untouched"
    if app == "radarr":
        await src.arr(i, "PUT", "/movie/editor", json={"movieIds": [arr["id"]], "monitored": False})
        return f"radarr #{arr['id']} unmonitored"
    if it["kind"] == "show":
        await src.arr(i, "PUT", "/series/editor", json={"seriesIds": [arr["id"]], "monitored": False})
        return f"sonarr #{arr['id']} unmonitored"
    # season: flip the season flag, then the episodes explicitly
    idx = it["season_index"]
    s = await src.arr(i, "GET", f"/series/{arr['id']}")
    for x in s.get("seasons") or []:
        if x["seasonNumber"] == idx:
            x["monitored"] = False
    await src.arr(i, "PUT", f"/series/{arr['id']}", json=s)
    eps = await src.arr(i, "GET", "/episode", params={"seriesId": arr["id"], "seasonNumber": idx})
    ids = [e["id"] for e in eps or []]
    if ids:
        await src.arr(i, "PUT", "/episode/monitor", json={"episodeIds": ids, "monitored": False})
    return f"sonarr #{arr['id']} season {idx} unmonitored ({len(ids)} eps)"


def remove_from_raw(raw, it):
    """Mirror a confirmed Plex delete in the cached upstream data."""
    k = it["key"]
    if it["kind"] == "movie":
        raw["movies"] = [m for m in raw["movies"] if m["key"] != k]
    elif it["kind"] == "version":
        for m in raw["movies"]:
            if m["key"] == it["title_key"]:
                m["media"] = [x for x in m["media"] if x["id"] != it["media_id"]]
    elif it["kind"] == "show":
        raw["shows"] = [s for s in raw["shows"] if s["key"] != k]
        raw["seasons"] = [s for s in raw["seasons"] if s["show"] != k]
        raw["episodes"] = [e for e in raw["episodes"] if e["show"] != k]
    elif it["kind"] == "season":
        raw["seasons"] = [s for s in raw["seasons"] if s["key"] != k]
        raw["episodes"] = [e for e in raw["episodes"] if e["season"] != k]
        show = it["title_key"]
        if not any(e["show"] == show for e in raw["episodes"]):
            raw["shows"] = [s for s in raw["shows"] if s["key"] != show]


def snapshot_meta(model, it):
    """What we knew about it at the moment of deletion."""
    t = model.titles.get(it["title_key"]) or {}
    d = model.detail(it["title_key"]) or {}
    if it["kind"] == "season":
        files = sorted((model.seasons.get(it["key"]) or {}).get("files", {}).keys())
    elif it["kind"] == "version":
        files = next((v["files"] for v in t.get("versions", []) if v["id"] == it["media_id"]), [])
    elif t.get("kind") == "movie":
        files = [f for v in t.get("versions", []) for f in v["files"]]
    else:
        files = sorted(f for f, (tk, _) in model.files.items() if tk == it["title_key"])
    return {"title": t.get("title"), "year": t.get("year"), "kind": t.get("kind"), "ids": t.get("ids"),
            "res": t.get("res"), "added": t.get("added"), "folder": t.get("folder"),
            "files": files, "nfiles": len(files),
            "viewers": [{"name": u["name"], "plays": u["plays"], "last": u["last"]} for u in d.get("users", [])],
            "requested": [{"name": r.get("name"), "at": r.get("at")} for r in d.get("requests", [])],
            "arr": t.get("arr")}


async def run(src, model, raw, items, do_unmonitor, progress):
    """Delete items one at a time, yielding (item, result, meta). Mutates raw."""
    inst = {}
    if do_unmonitor:
        inst = await src.arr_instances()
    for n, it in enumerate(items, 1):
        progress(f"{n}/{len(items)} {it['label']}")
        title = model.titles.get(it["title_key"]) or {}
        meta = snapshot_meta(model, it)
        res = {"id": it.get("id"), "label": it["label"], "kind": it["kind"], "bytes": it["bytes"], "ok": False}
        try:
            status, body = await src.plex_delete(plex_path(it))
            res["plex"] = f"HTTP {status}"
            if status not in (200, 204):
                res["error"] = f"Plex refused: HTTP {status} {body}".strip()
            else:
                if it["kind"] != "version" and await src.plex_exists(it["key"]):
                    res["error"] = "Plex said OK but the item is still in the library"
                else:
                    res["ok"] = True
                    remove_from_raw(raw, it)
                    if do_unmonitor:
                        try:
                            res["arr"] = await unmonitor(src, inst, it, title)
                        except Exception as ex:  # file is gone either way; surface the arr miss
                            res["arr"] = f"unmonitor FAILED: {ex}"
        except Exception as ex:
            res["error"] = f"{type(ex).__name__}: {ex}"
        log.info("delete %s %s -> %s", it["kind"], it["label"], res)
        yield it, res, meta
