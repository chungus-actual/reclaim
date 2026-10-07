"""Downgrade: have Radarr/Sonarr replace a file with a smaller, lower-quality release.

The *arrs only ever upgrade. Verified in source (Radarr v6.3.0.10514, Sonarr v4.0.19.2979):
- Quality rank = position in the profile's item list (QualityProfile.GetIndex), allowed or not.
- The import-time UpgradeSpecification rejects only when the new quality ranks BELOW the
  existing file; no import spec looks at upgradeAllowed.
- The search-side UpgradeAllowedSpecification rejects every upgrade when upgradeAllowed=false.

So a "Reclaim ↓720p" profile that ranks everything above 720p at the very bottom makes a
720p file an "upgrade" at import (it replaces the old file, no gap), while upgradeAllowed=false
keeps RSS from grabbing anything on its own. We pick the exact release (interactive search),
grab it by guid, and watch for the import.
"""
import logging
import re
import time

log = logging.getLogger("reclaim.downgrade")

TIERS = (1080, 720)
BIG_NAMES = re.compile(r"remux|br-disk|raw-hd", re.I)
EXTRAS = re.compile(r"(?<![a-z])(extras?|specials?|featurettes?|bonus|behind[ ._-]the[ ._-]scenes|sample|trailer)(?![a-z])", re.I)
# rejection text that only reflects quality ranking / the current profile / size rules —
# irrelevant once the item sits on the downsize profile. Everything else blocks.
BENIGN = re.compile(r"meets cutoff|not wanted in profile|equal or higher preference|does not allow upgrades|"
                    r"not an upgrade|custom format|smaller than minimum allowed|larger than maximum allowed", re.I)
SIZE_RULE = re.compile(r"smaller than minimum allowed|larger than maximum allowed", re.I)
SIZE_RULE_PARTS = re.compile(r"is (smaller|larger) than (?:minimum|maximum) allowed ([\d.]+ \w+)", re.I)
# language tags on releases disagree between indexers for the same post, so read the name too
MULTI_AUDIO = re.compile(r"(?<![a-z])(dual|multi|[a-z]{2}-en|en-[a-z]{2})(?![a-z])", re.I)
MIN_SAVE = 0.25
MB_PER_MIN_FLOOR = {1080: 6.0, 720: 4.0}   # below this a "pack" is usually extras, samples or junk


def profile_name(target):
    return f"Reclaim ↓{target}p"


def _res(item):
    if item.get("quality"):
        return item["quality"].get("resolution") or 0
    return max((_res(x) for x in item.get("items") or []), default=0)


def _name(item):
    return item.get("name") or (item.get("quality") or {}).get("name") or ""


def _big(item, target):
    return _res(item) > target or bool(BIG_NAMES.search(_name(item))) or any(
        BIG_NAMES.search(_name(x)) for x in item.get("items") or [])


def build_profile(base, target):
    """Downsize profile from the app's all-qualities profile: big qualities ranked lowest."""
    items = []
    for it in base["items"]:
        it = dict(it)
        allowed = (_res(it) == target) and not _big(it, target)
        it["allowed"] = allowed
        if it.get("items"):
            it["items"] = [dict(x, allowed=allowed) for x in it["items"]]
        items.append(it)
    big = [i for i in items if _big(i, target)]
    rest = [i for i in items if not _big(i, target)]
    ordered = big + rest                      # index 0 = lowest preference
    allowed = [i for i in ordered if i["allowed"]]
    if not allowed:
        raise RuntimeError(f"no {target}p qualities in the base profile")
    top = allowed[-1]
    out = {k: v for k, v in base.items() if k not in ("id", "items")}
    out.update({
        "name": profile_name(target),
        "items": ordered,
        "cutoff": top["quality"]["id"] if top.get("quality") else top["id"],
        "upgradeAllowed": False,
        "minFormatScore": 0, "cutoffFormatScore": 0, "minUpgradeFormatScore": 1,
        "formatItems": [dict(f, score=0) for f in base.get("formatItems") or []],
    })
    return out


async def ensure_profile(src, inst, target):
    profiles = await src.arr(inst, "GET", "/qualityprofile")
    name = profile_name(target)
    base = next((p for p in profiles if p["name"] == "Any"), None) or max(profiles, key=lambda p: len(p["items"]))
    want = build_profile(base, target)
    have = next((p for p in profiles if p["name"] == name), None)
    if have:
        want["id"] = have["id"]
        await src.arr(inst, "PUT", f"/qualityprofile/{have['id']}", json=want)
        return have["id"]
    made = await src.arr(inst, "POST", "/qualityprofile", json=want)
    return made["id"]


# ------------------------------------------------------------------ search
def classify(rel, target, current_bytes, runtime_min, season, app="arr"):
    q = rel["quality"]["quality"]
    blockers = [r for r in rel.get("rejections") or [] if not BENIGN.search(r)]
    warnings = []
    for r in rel.get("rejections") or []:
        m = SIZE_RULE_PARTS.search(r)
        if m:
            warnings.append(f"{'under' if m.group(1).lower() == 'smaller' else 'over'} your {app} size rule ({m.group(2)})")
    mbpm = rel["size"] / 1e6 / runtime_min if runtime_min else None
    floor = MB_PER_MIN_FLOOR.get(target, 3.0)
    if EXTRAS.search(rel.get("title") or ""):
        blockers.append("looks like extras/specials, not the episodes")
    if mbpm is not None and mbpm < floor * 0.4:
        blockers.append(f"{mbpm:.1f} MB/min — too small to be the whole thing")
    elif mbpm is not None and mbpm < floor:
        warnings.append(f"low bitrate ({mbpm:.1f} MB/min)")
    if re.search(r"(?<![a-z0-9])av1(?![a-z0-9])", rel.get("title") or "", re.I):
        warnings.append("AV1 — older Plex clients have to transcode it")
    langs = [l.get("name") for l in rel.get("languages") or [] if l.get("name")]
    multi = len(langs) > 1 or bool(MULTI_AUDIO.search(rel.get("title") or ""))
    saves = current_bytes - rel["size"]
    return {
        "guid": rel["guid"], "indexerId": rel["indexerId"], "indexer": rel.get("indexer"),
        "title": rel.get("title"), "quality": q["name"], "resolution": q.get("resolution"),
        "size": rel["size"], "saves": saves, "saves_pct": saves / current_bytes if current_bytes else 0,
        "mbpm": round(mbpm, 1) if mbpm else None, "age_days": rel.get("age"), "protocol": rel.get("protocol"),
        "weight": rel.get("releaseWeight", 0), "blockers": blockers, "warnings": warnings,
        "languages": langs, "multi": multi, "edition": rel.get("edition") or None,
        "full_season": rel.get("fullSeason"), "ok": not blockers and saves > 0,
    }


def candidates(releases, target, current_bytes, runtime_min, season=None, app="arr", prefer_multi=False,
               need_saving=True):
    """need_saving=False is a replacement for something Plex can't play (a disc rip, a file it
    never matched): any clean release at the tier will do, smaller or not."""
    out = []
    for rel in releases:
        q = rel["quality"]["quality"]
        if q.get("resolution") != target or BIG_NAMES.search(q["name"]):
            continue
        if season is not None and not (rel.get("fullSeason") and rel.get("seasonNumber") == season):
            continue
        c = classify(rel, target, current_bytes, runtime_min, season, app)
        if not need_saving:
            c["ok"] = not c["blockers"]
        out.append(c)
    floor = MB_PER_MIN_FLOOR.get(target, 3.0)
    eligible = [c for c in out if c["ok"] and (c["saves_pct"] >= MIN_SAVE or not need_saving)
                and (c["mbpm"] is None or c["mbpm"] >= floor)]
    # the arr's own preference among sane picks; multi-audio last unless the series wants it (anime)
    rec = min(eligible, key=lambda c: (c["multi"] != prefer_multi, c["weight"]), default=None)
    for c in out:
        c["recommended"] = c is rec
    out.sort(key=lambda c: (not c["ok"], c["weight"]))
    return out


async def movie_context(src, inst, movie_id):
    m = await src.arr(inst, "GET", f"/movie/{movie_id}")
    f = m.get("movieFile") or {}
    return {"arr_title": m.get("title"), "profile": m.get("qualityProfileId"), "monitored": m.get("monitored"),
            "file_id": f.get("id"), "file_size": f.get("size") or 0,
            "file_quality": ((f.get("quality") or {}).get("quality") or {}).get("name"),
            "file_res": ((f.get("quality") or {}).get("quality") or {}).get("resolution"),
            "file": f.get("relativePath"), "runtime": m.get("runtime")}


async def season_context(src, inst, series_id, season):
    s = await src.arr(inst, "GET", f"/series/{series_id}")
    files = [f for f in await src.arr(inst, "GET", "/episodefile", params={"seriesId": series_id})
             if f.get("seasonNumber") == season]
    quals = {}
    for f in files:
        n = ((f.get("quality") or {}).get("quality") or {}).get("name")
        quals[n] = quals.get(n, 0) + 1
    return {"arr_title": s.get("title"), "profile": s.get("qualityProfileId"), "monitored": s.get("monitored"),
            "series_type": s.get("seriesType"),
            "file_ids": sorted(f["id"] for f in files), "file_size": sum(f.get("size") or 0 for f in files),
            "file_quality": max(quals, key=quals.get) if quals else None,
            "file_res": max((((f.get("quality") or {}).get("quality") or {}).get("resolution") or 0) for f in files) if files else None,
            "files": len(files)}


# -------------------------------------------------------------------- grab
async def set_profile(src, inst, app, item_id, profile_id):
    if app == "radarr":
        await src.arr(inst, "PUT", "/movie/editor", json={"movieIds": [item_id], "qualityProfileId": profile_id})
    else:
        await src.arr(inst, "PUT", "/series/editor", json={"seriesIds": [item_id], "qualityProfileId": profile_id})


async def grab(src, inst, cand):
    return await src.arr(inst, "POST", "/release", json={"guid": cand["guid"], "indexerId": cand["indexerId"]})


# -------------------------------------------------------------------- poll
async def check(src, inst, job):
    """Advance one job. Returns (state, note, new_size)."""
    app, item = job["app"], job["item_id"]
    if app == "radarr":
        ctx = await movie_context(src, inst, item)
        if ctx["file_id"] and ctx["file_id"] != job["old_file_id"]:
            return "imported", f"now {ctx['file_quality']}", ctx["file_size"]
        queue = await src.arr(inst, "GET", "/queue/details", params={"movieId": item})
    else:
        ctx = await season_context(src, inst, item, job["season"])
        old = set(int(x) for x in (job["old_file_ids"] or "").split(",") if x)
        new = set(ctx["file_ids"])
        if new and not (new & old):
            return "imported", f"now {ctx['file_quality']} · {ctx['files']} files", ctx["file_size"]
        if new - old and ctx["file_res"] is not None:
            partial = f"{len(new - old)} of {len(new)} episode files replaced so far"
        else:
            partial = None
        queue = await src.arr(inst, "GET", "/queue/details", params={"seriesId": item})
        queue = [q for q in queue or [] if q.get("seasonNumber") in (None, job["season"])
                 or (q.get("episode") or {}).get("seasonNumber") == job["season"]]
        if partial and not queue:
            return "imported", partial + " (pack had fewer episodes)", ctx["file_size"]
    if queue:
        q = queue[0]
        state = (q.get("trackedDownloadState") or "").lower()
        msgs = "; ".join(m for s in q.get("statusMessages") or [] for m in s.get("messages") or [])
        size, left = q.get("size") or 0, q.get("sizeleft") or 0
        pct = f"{100 * (1 - left / size):.0f}%" if size else ""
        if state in ("failed", "failedpending"):
            return "failed", msgs or q.get("errorMessage") or "download failed", None
        if state in ("importblocked", "importpending") and msgs:
            return "grabbed", f"import waiting: {msgs}", None
        return "grabbed", f"{q.get('status', 'queued')} {pct}".strip(), None
    # nothing queued and no new file: look for a failure in history after the grab
    if time.time() - job["created"] > 600:
        path = "/history/movie" if app == "radarr" else "/history/series"
        params = {"movieId": item} if app == "radarr" else {"seriesId": item, "seasonNumber": job["season"]}
        hist = await src.arr(inst, "GET", path, params=params) or []
        for h in hist:
            if h.get("eventType") in ("downloadFailed", "downloadIgnored") and _ts(h.get("date")) > job["created"]:
                return "failed", (h.get("data") or {}).get("message") or h["eventType"], None
        if time.time() - job["created"] > 3 * 86400:
            return "failed", "nothing downloaded or imported in 3 days", None
    return "grabbed", "waiting for the download client", None


def _ts(iso):
    import datetime as dt
    try:
        return dt.datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0
