"""In-app settings: the schema the UI renders, saving, and a real test per service.

Every test talks to the actual service and reports lines of (level, text):
"ok" / "warn" / "error". Tests never change anything on the other side.
"""
import asyncio
import json
import os
import random
import shutil
import urllib.parse

import httpx

import config as C

GROUPS = [
    {"id": "plex", "title": "Plex", "required": True, "test": True,
     "blurb": "The server to audit. Needs the server owner's token: watch history and deletes depend on it.",
     "fields": [
         ("PLEX_URL", "Server URL", "http://192.0.2.10:32400", "Address reclaim uses to reach Plex."),
         ("PLEX_TOKEN", "Token", "", "Use “Sign in with Plex”, or paste the owner's X-Plex-Token."),
     ]},
    {"id": "tautulli", "title": "Tautulli", "test": True,
     "blurb": "Optional, recommended. Adds partial plays, watch time and per-person detail. Without it only completed views count.",
     "fields": [
         ("TAUTULLI_URL", "URL", "http://192.0.2.10:8181", ""),
         ("TAUTULLI_API_KEY", "API key", "", "Tautulli → Settings → Web Interface → API key."),
     ]},
    {"id": "radarr", "title": "Radarr", "test": True,
     "blurb": "Optional. Unmonitors deleted movies so they aren't downloaded again, and powers movie downgrades.",
     "fields": [
         ("RADARR_URL", "URL", "http://192.0.2.10:7878", ""),
         ("RADARR_API_KEY", "API key", "", "Radarr → Settings → General → API Key."),
     ]},
    {"id": "sonarr", "title": "Sonarr", "test": True,
     "blurb": "Optional. Unmonitors deleted shows and seasons, and powers TV downgrades.",
     "fields": [
         ("SONARR_URL", "URL", "http://192.0.2.10:8989", ""),
         ("SONARR_API_KEY", "API key", "", "Sonarr → Settings → General → API Key."),
     ]},
    {"id": "seerr", "title": "Overseerr / Jellyseerr", "test": True,
     "blurb": "Optional. Shows who requested each title. Radarr/Sonarr left blank above are picked up from here.",
     "fields": [
         ("SEERR_URL", "URL", "http://192.0.2.10:5055", ""),
         ("SEERR_API_KEY", "API key", "", "Settings → General → API Key."),
     ]},
    {"id": "capacity", "title": "Free space", "test": True,
     "blurb": "Optional. Either your Unraid server's API, or folders inside reclaim's container that sit on the media disks.",
     "fields": [
         ("UNRAID_URL", "Unraid API URL", "http://tower/graphql", "Unraid 7+: Settings → Management Access → API Keys."),
         ("UNRAID_API_KEY", "Unraid API key", "", "Needs read access to the array."),
         ("CAPACITY_PATHS", "…or folders on the media disks", "/media", "Used when Unraid isn't set. Each disk is counted once."),
     ]},
    {"id": "walk", "title": "Disk walk", "test": True,
     "blurb": "Optional. Mount your media read-only into reclaim's container, then map each Plex library folder to where it appears here. "
              "Finds ISOs, rips and leftovers that Plex doesn't index.",
     "fields": [
         ("WALK_PATHS", "Folders", "", "Left: the folder as Plex sees it. Right: the same folder inside reclaim."),
         ("WALK_AFTER_REFRESH", "Walk after every rebuild", "", ""),
         ("DISPLAY_PATHS", "Show paths as", "", "Optional: rewrite paths for display and copying, e.g. /data → /mnt/user/media."),
     ]},
    {"id": "safety", "title": "Safety & access", "test": False,
     "blurb": "Read-only turns off deleting and downgrading. A login protects everything (use a TLS proxy if this is reachable from outside).",
     "fields": [
         ("RECLAIM_READ_ONLY", "Read-only mode", "", ""),
         ("RECLAIM_USER", "Login user", "", "Leave both blank for no login."),
         ("RECLAIM_PASSWORD", "Login password", "", "Stored hashed."),
     ]},
    {"id": "schedule", "title": "Schedule", "test": False,
     "blurb": "reclaim rebuilds its picture of the library every night, and whenever you press Refresh.",
     "fields": [
         ("RECLAIM_REFRESH_HOUR", "Nightly rebuild hour (0–23)", "4", "In the container's timezone (TZ)."),
     ]},
]


def payload():
    """Schema + current values for the UI. Secrets never leave the server; only whether they're set."""
    eff = C.effective()
    groups = []
    for g in GROUPS:
        fields = []
        for key, label, placeholder, help_ in g["fields"]:
            kind = C.FIELDS[key][1]
            v = eff[key]
            f = {"key": key, "label": label, "kind": kind, "placeholder": placeholder, "help": help_,
                 "locked": C.env_source(key)}
            if kind in C.SECRET_KINDS:
                f["set"] = bool(v)
            else:
                f["value"] = [list(p) for p in v] if kind == "pairs" else v
            fields.append(f)
        groups.append({**{k: g[k] for k in ("id", "title", "blurb", "test")}, "required": g.get("required", False),
                       "fields": fields})
    return {"groups": groups, "summary": C.summary(), "data_dir": str(C.DATA_DIR)}


def merged(values, clear=()):
    """Current saved settings overlaid with what the form sent (blank secret = keep)."""
    saved = C.load_saved()
    out = dict(saved)
    for key, raw in (values or {}).items():
        if key not in C.FIELDS or C.env_source(key):
            continue
        kind = C.FIELDS[key][1]
        if kind in C.SECRET_KINDS and (raw is None or str(raw) == ""):
            continue
        out[key] = raw
    for key in clear:
        if key in C.FIELDS and C.FIELDS[key][1] in C.SECRET_KINDS:
            out.pop(key, None)
    return out


def validate(data):
    """Type-check everything that would be saved. Returns {key: reason} for bad ones."""
    bad = {}
    for key, raw in data.items():
        if key.startswith("_") or key not in C.FIELDS:
            continue
        kind = C.FIELDS[key][1]
        if kind == "password":
            continue
        try:
            v = C.parse(kind, raw)
            if key == "RECLAIM_REFRESH_HOUR" and not 0 <= v <= 23:
                bad[key] = "must be 0–23"
        except C.Invalid as ex:
            bad[key] = str(ex)
    user = data.get("RECLAIM_USER", "")
    if bool(str(user).strip()) != bool(data.get("RECLAIM_PASSWORD")) and not C.env_source("RECLAIM_USER"):
        bad["RECLAIM_PASSWORD" if user else "RECLAIM_USER"] = "set both user and password, or neither"
    return bad


def save(values, clear=()):
    data = merged(values, clear)
    pw = (values or {}).get("RECLAIM_PASSWORD")
    if pw and not C.env_source("RECLAIM_PASSWORD"):
        data["RECLAIM_PASSWORD"] = C.hash_password(str(pw))
    if "RECLAIM_USER" in (values or {}) and not str(values["RECLAIM_USER"]).strip():
        data.pop("RECLAIM_PASSWORD", None)          # clearing the user turns the login off
    bad = validate(data)
    if bad:
        return {"ok": False, "errors": bad}
    for key in list(data):
        if key in C.FIELDS and C.FIELDS[key][1] not in ("password",):
            data[key] = C.parse(C.FIELDS[key][1], data[key])
            if C.FIELDS[key][1] == "pairs":
                data[key] = [list(p) for p in data[key]]
    C.write_saved(data)
    C.reload()
    return {"ok": True}


def candidate(values):
    """Typed settings as they'd be after saving the form, for testing before saving."""
    data = merged(values)
    out = {}
    for key, (_attr, kind, default, _alt) in C.FIELDS.items():
        src = C.env_source(key)
        raw = os.environ[src] if src else data.get(key, default)
        try:
            out[key] = C.parse(kind, raw) if kind != "password" else raw
        except C.Invalid as ex:
            out[key] = C.Invalid(str(ex))
    return out


# ---------------------------------------------------------------------- tests
def _http():
    return httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=6.0), follow_redirects=True)


def _plex_headers(token=None):
    h = {"Accept": "application/json", "X-Plex-Product": "reclaim", "X-Plex-Client-Identifier": C.client_id()}
    if token:
        h["X-Plex-Token"] = token
    return h


def _why(ex):
    if isinstance(ex, httpx.ConnectTimeout):
        return "timed out connecting"
    if isinstance(ex, httpx.ConnectError):
        return "couldn't connect (wrong address, or not reachable from reclaim's container)"
    if isinstance(ex, httpx.ReadTimeout):
        return "connected but it didn't answer in time"
    return f"{type(ex).__name__}: {ex}"


async def test(group, values, model=None):
    s = candidate(values)
    bad = [f"{k}: {v}" for k, v in s.items() if isinstance(v, C.Invalid)]
    if bad:
        return {"ok": False, "lines": [["error", b] for b in bad]}
    fn = {"plex": test_plex, "tautulli": test_tautulli, "radarr": test_radarr, "sonarr": test_sonarr,
          "seerr": test_seerr, "capacity": test_capacity, "walk": test_walk}.get(group)
    if not fn:
        return {"ok": False, "lines": [["error", f"nothing to test for {group}"]]}
    lines, data = [], {}
    try:
        await fn(s, lines, data, model)
    except Exception as ex:  # a test must always come back with something readable
        lines.append(["error", _why(ex)])
    return {"ok": not any(l[0] == "error" for l in lines), "lines": lines, "data": data}


async def test_plex(s, lines, data, model):
    url, token = s["PLEX_URL"], s["PLEX_TOKEN"]
    if not url:
        lines.append(["error", "Enter the server URL."])
        return
    async with _http() as http:
        try:
            r = await http.get(url + "/identity", headers=_plex_headers())
        except Exception as ex:
            lines.append(["error", f"{url}: {_why(ex)}"])
            return
        if r.status_code != 200 or "MediaContainer" not in r.text:
            lines.append(["error", f"{url} answered HTTP {r.status_code}, but not like a Plex server."])
            return
        ident = r.json()["MediaContainer"]
        data["machine"] = ident.get("machineIdentifier")
        r = await http.get(url + "/", headers=_plex_headers(token))
        if r.status_code == 401:
            lines.append(["error", "Plex refused the token." if token else "Plex wants a token: sign in with Plex or paste one."])
            return
        root = r.json()["MediaContainer"]
        data["name"] = root.get("friendlyName")
        lines.append(["ok", f"Reached “{root.get('friendlyName')}” (Plex {ident.get('version', '?').split('-')[0]})."])
        if not token:
            lines.append(["warn", "No token: works only while this host is in Plex's “allowed without auth” networks."])
        r = await http.get(url + "/library/sections", headers=_plex_headers(token))
        secs = [d for d in r.json()["MediaContainer"].get("Directory", []) if d["type"] in ("movie", "show")]
        data["sections"] = [{"title": d["title"], "type": d["type"], "roots": [l["path"] for l in d.get("Location", [])]}
                            for d in secs]
        if secs:
            lines.append(["ok", "Libraries: " + ", ".join(f"{d['title']} ({'movies' if d['type'] == 'movie' else 'TV'})" for d in secs)])
        else:
            lines.append(["error", "No movie or TV libraries on this server."])
        r = await http.get(url + "/status/sessions/history/all", headers=_plex_headers(token),
                           params={"X-Plex-Container-Start": 0, "X-Plex-Container-Size": 1})
        if r.status_code == 200:
            lines.append(["ok", f"Can read watch history ({r.json()['MediaContainer'].get('totalSize', 0):,} plays)."])
        else:
            lines.append(["error", "This token can't read watch history — use the server owner's token."])
        r = await http.get(url + "/:/prefs", headers=_plex_headers(token))
        if r.status_code == 200:
            prefs = {p["id"]: p.get("value") for p in r.json()["MediaContainer"].get("Setting", [])}
            if str(prefs.get("allowMediaDeletion")).lower() in ("true", "1"):
                lines.append(["ok", "Media deletion is allowed."])
            else:
                lines.append(["warn", "Media deletion is off in Plex (Settings → Library → Allow media deletion). "
                                      "Browsing works; deleting won't."])


async def test_tautulli(s, lines, data, model):
    url, key = s["TAUTULLI_URL"], s["TAUTULLI_API_KEY"]
    if not (url and key):
        lines.append(["error", "Enter the URL and API key."])
        return
    async with _http() as http:
        try:
            r = await http.get(url + "/api/v2", params={"apikey": key, "cmd": "get_tautulli_info"})
        except Exception as ex:
            lines.append(["error", f"{url}: {_why(ex)}"])
            return
        try:
            resp = r.json()["response"]
        except (ValueError, KeyError):
            lines.append(["error", f"{url} answered HTTP {r.status_code}, but not like Tautulli."])
            return
        if resp.get("result") != "success":
            lines.append(["error", f"Tautulli said: {resp.get('message') or 'error'}."])
            return
        lines.append(["ok", f"Tautulli {resp['data'].get('tautulli_version', '?')}."])
        r = await http.get(url + "/api/v2", params={"apikey": key, "cmd": "get_server_identity"})
        mid = (r.json()["response"].get("data") or {}).get("machine_identifier")
        plex_mid = None
        if s["PLEX_URL"]:
            try:
                rr = await http.get(s["PLEX_URL"] + "/identity", headers=_plex_headers())
                plex_mid = rr.json()["MediaContainer"].get("machineIdentifier")
            except Exception:
                pass
        if mid and plex_mid and mid != plex_mid:
            lines.append(["error", "This Tautulli watches a different Plex server than the one above."])
        elif mid and plex_mid:
            lines.append(["ok", "Watches the same Plex server."])
        # (not get_history: counting a big history can take Tautulli 20 s+ on a cold cache)
        try:
            r = await http.get(url + "/api/v2", params={"apikey": key, "cmd": "get_users"})
            users = r.json()["response"].get("data") or []
            lines.append(["ok", f"Knows {len(users)} people."])
        except (httpx.HTTPError, ValueError, KeyError):
            pass


async def _test_arr(s, lines, data, want, url, key):
    if not (url and key):
        lines.append(["error", "Enter the URL and API key."])
        return
    base = url if url.endswith("/api/v3") else url + "/api/v3"
    h = {"X-Api-Key": key}
    async with _http() as http:
        try:
            r = await http.get(base + "/system/status", headers=h)
        except Exception as ex:
            lines.append(["error", f"{url}: {_why(ex)}"])
            return
        if r.status_code == 401:
            lines.append(["error", "Wrong API key."])
            return
        if r.status_code != 200:
            lines.append(["error", f"{url} answered HTTP {r.status_code} — is that the {want} address (and its URL base)?"])
            return
        st = r.json()
        app = st.get("appName") or st.get("instanceName") or "?"
        if app.lower() != want.lower():
            lines.append(["error", f"That's {app}, not {want}."])
            return
        ver = st.get("version", "?")
        lines.append(["ok", f"{want} {ver}."])
        if want == "Sonarr" and ver.split(".")[0].isdigit() and int(ver.split(".")[0]) < 4:
            lines.append(["warn", "Sonarr v3 is untested; v4 or newer is recommended."])
        mm = (await http.get(base + "/config/mediamanagement", headers=h)).json()
        flag = "autoUnmonitorPreviouslyDownloadedMovies" if want == "Radarr" else "autoUnmonitorPreviouslyDownloadedEpisodes"
        if mm.get(flag) is False:
            lines.append(["ok", f"“Unmonitor deleted {'movies' if want == 'Radarr' else 'episodes'}” is off in {want} — "
                                "reclaim unmonitors each title it deletes, so nothing gets re-downloaded."])
        profiles = (await http.get(base + "/qualityprofile", headers=h)).json()
        mine = [p["name"] for p in profiles if p["name"].startswith("Reclaim ↓")]
        lines.append(["ok", f"{len(profiles)} quality profiles" + (f" (incl. {', '.join(mine)})" if mine else "") + "."])


async def test_radarr(s, lines, data, model):
    await _test_arr(s, lines, data, "Radarr", s["RADARR_URL"], s["RADARR_API_KEY"])


async def test_sonarr(s, lines, data, model):
    await _test_arr(s, lines, data, "Sonarr", s["SONARR_URL"], s["SONARR_API_KEY"])


async def test_seerr(s, lines, data, model):
    url, key = s["SEERR_URL"], s["SEERR_API_KEY"]
    if not (url and key):
        lines.append(["error", "Enter the URL and API key."])
        return
    async with _http() as http:
        try:
            r = await http.get(url + "/api/v1/status")
        except Exception as ex:
            lines.append(["error", f"{url}: {_why(ex)}"])
            return
        if r.status_code != 200 or "version" not in r.text:
            lines.append(["error", f"{url} answered HTTP {r.status_code}, but not like Overseerr/Jellyseerr."])
            return
        ver = r.json().get("version")
        h = {"X-Api-Key": key}
        r = await http.get(url + "/api/v1/request", headers=h, params={"take": 1})
        if r.status_code in (401, 403):
            lines.append(["error", "Wrong API key."])
            return
        lines.append(["ok", f"Version {ver} · {r.json()['pageInfo']['results']:,} requests."])
        for app in ("radarr", "sonarr"):
            if s[f"{app.upper()}_URL"]:
                continue
            rows = (await http.get(f"{url}/api/v1/settings/{app}", headers=h)).json() or []
            rows = [x for x in rows if not x.get("is4k")] or rows
            if rows:
                x = next((y for y in rows if y.get("isDefault")), rows[0])
                lines.append(["ok", f"Will use its {app.capitalize()}: {x['hostname']}:{x['port']}."])


async def test_capacity(s, lines, data, model):
    if s["UNRAID_URL"] or s["UNRAID_API_KEY"]:
        if not (s["UNRAID_URL"] and s["UNRAID_API_KEY"]):
            lines.append(["error", "Unraid needs both the URL and the API key."])
            return
        q = "{ array { state capacity { kilobytes { free used total } } } }"
        async with _http() as http:
            try:
                r = await http.post(s["UNRAID_URL"], headers={"x-api-key": s["UNRAID_API_KEY"]}, json={"query": q})
            except Exception as ex:
                lines.append(["error", f"{s['UNRAID_URL']}: {_why(ex)}"])
                return
        try:
            d = r.json()
        except ValueError:
            lines.append(["error", f"HTTP {r.status_code}, not a GraphQL answer — the URL usually ends in /graphql."])
            return
        if d.get("errors"):
            msg = d["errors"][0].get("message", "")
            lines.append(["error", "This API key can't read the array." if "orbidden" in msg else f"Unraid said: {msg}"])
            return
        kb = d["data"]["array"]["capacity"]["kilobytes"]
        total, free = int(kb["total"]) * 1000, int(kb["free"]) * 1000
        lines.append(["ok", f"Array {d['data']['array']['state'].lower()}: {free / 1e12:.2f} TB free of {total / 1e12:.2f} TB."])
        return
    if not s["CAPACITY_PATHS"]:
        lines.append(["error", "Set Unraid, or at least one folder."])
        return
    seen = set()
    total = 0
    for p in s["CAPACITY_PATHS"]:
        if not os.path.isdir(p):
            lines.append(["error", f"{p}: not a folder inside reclaim's container."])
            continue
        dev = os.stat(p).st_dev
        u = shutil.disk_usage(p)
        if dev in seen:
            lines.append(["ok", f"{p}: same disk as an earlier folder (counted once)."])
            continue
        seen.add(dev)
        total += u.total
        lines.append(["ok", f"{p}: {u.free / 1e12:.2f} TB free of {u.total / 1e12:.2f} TB."])
    if model and total and total < sum(model.lib_bytes.values()) * 0.95:
        lines.append(["warn", f"That's less than the {sum(model.lib_bytes.values()) / 1e12:.1f} TB Plex indexes — "
                              "these folders probably aren't on the media disks."])


async def test_walk(s, lines, data, model):
    pairs = s["WALK_PATHS"]
    if not pairs:
        lines.append(["error", "Add at least one folder pair."])
        return
    roots = []
    if model:
        roots = [r.rstrip("/") for sec in model.sections.values() for r in sec["roots"]]
    for plex, local in pairs:
        if not os.path.isdir(local):
            lines.append(["error", f"{local}: not found inside reclaim's container — is the media mounted there?"])
            continue
        try:
            n = len(os.listdir(local))
        except OSError as ex:
            lines.append(["error", f"{local}: can't read it ({ex.strerror})."])
            continue
        if roots and not any(plex == r or plex.startswith(r + "/") or r.startswith(plex + "/") for r in roots):
            lines.append(["warn", f"{plex} doesn't match a Plex library folder. Plex uses: {', '.join(roots)}."])
        if model:
            under = [f for f in model.files if f.startswith(plex + "/")]
            sample = random.sample(under, min(5, len(under)))
            if not sample:
                lines.append(["warn", f"{plex} → {local}: {n} entries, but no Plex files live under {plex}."])
                continue
            found = sum(1 for f in sample if os.path.exists(local + f[len(plex):]))
            level = "ok" if found == len(sample) else ("warn" if found else "error")
            lines.append([level, f"{plex} → {local}: {found} of {len(sample)} sample files from Plex found."])
        else:
            lines.append(["ok", f"{local}: readable, {n} entries. (Build the library once to check it against Plex.)"])


# ---------------------------------------------------------------- plex sign-in
async def plex_pin():
    async with _http() as http:
        r = await http.post("https://plex.tv/api/v2/pins", params={"strong": "true"}, headers=_plex_headers())
        r.raise_for_status()
        pin = r.json()
    q = urllib.parse.urlencode({"clientID": C.client_id(), "code": pin["code"], "context[device][product]": "reclaim"})
    return {"id": pin["id"], "auth_url": f"https://app.plex.tv/auth#?{q}", "expires_in": pin.get("expiresIn")}


async def plex_pin_check(pin_id):
    """Once the user approves: the token plus the servers it owns, with their addresses."""
    async with _http() as http:
        r = await http.get(f"https://plex.tv/api/v2/pins/{int(pin_id)}", headers=_plex_headers())
        r.raise_for_status()
        token = r.json().get("authToken")
        if not token:
            return {"done": False}
        r = await http.get("https://plex.tv/api/v2/resources", params={"includeHttps": 1, "includeRelay": 0},
                           headers=_plex_headers(token))
        r.raise_for_status()
    servers = []
    for res in r.json():
        if "server" not in (res.get("provides") or ""):
            continue
        conns = []
        for c in res.get("connections") or []:
            if c.get("IPv6") or not c.get("uri"):
                continue
            if c.get("local") and c.get("address"):
                # plain http on the LAN address is the simplest thing that works from a container
                conns.append({"uri": f"http://{c['address']}:{c.get('port', 32400)}", "local": True, "relay": False})
            conns.append({"uri": c["uri"], "local": bool(c.get("local")), "relay": bool(c.get("relay"))})
        seen, uniq = set(), []
        for c in sorted(conns, key=lambda c: (not c["local"], c["relay"], not c["uri"].startswith("http://"))):
            if c["uri"] not in seen:
                seen.add(c["uri"])
                uniq.append(c)
        servers.append({"name": res.get("name"), "owned": bool(res.get("owned")),
                        "token": res.get("accessToken") or token, "connections": uniq})
    servers.sort(key=lambda x: not x["owned"])
    return {"done": True, "servers": servers}


# -------------------------------------------------------------------- browse
def browse(path):
    """Folders (only) under a path inside the container, to help fill in mappings."""
    path = os.path.normpath(path or "/")
    if not os.path.isdir(path):
        return {"path": path, "error": "not a folder", "dirs": []}
    try:
        dirs = sorted(e.name for e in os.scandir(path) if e.is_dir(follow_symlinks=True) and not e.name.startswith("."))
    except OSError as ex:
        return {"path": path, "error": ex.strerror, "dirs": []}
    return {"path": path, "parent": os.path.dirname(path) if path != "/" else None, "dirs": dirs[:300],
            "more": max(0, len(dirs) - 300)}
