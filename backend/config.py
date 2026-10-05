"""Settings: set in the app (Settings tab), or by environment variables / a .env file.

Precedence per setting: environment > saved in the app > default. A setting that comes from
the environment shows as locked in the UI. Values are module attributes (C.PLEX_URL, …) and
are re-applied live by reload() after the app saves, so callers must read them at use time.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent


def _load_env_file(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


for _p in (os.environ.get("RECLAIM_ENV_FILE"), ROOT / ".env", "/config/.env"):
    if _p and Path(_p).is_file():
        _load_env_file(Path(_p))

# bootstrap-only: where state lives and which port uvicorn binds (not editable in the app)
DATA_DIR = Path(os.environ.get("RECLAIM_DATA", str(ROOT / "data")).strip())
DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = DATA_DIR / "settings.json"
PORT = int(os.environ.get("RECLAIM_PORT", "8892"))

# key -> (attribute, kind, default, extra env names)
# kinds: url, str, secret, bool, int, list (comma separated), pairs (a=b, comma separated), password
FIELDS = {
    "PLEX_URL": ("PLEX_URL", "url", "", ()),
    "PLEX_TOKEN": ("PLEX_TOKEN", "secret", "", ()),
    "TAUTULLI_URL": ("TAUTULLI_URL", "url", "", ()),
    "TAUTULLI_API_KEY": ("TAUTULLI_API_KEY", "secret", "", ()),
    "RADARR_URL": ("RADARR_URL", "url", "", ()),
    "RADARR_API_KEY": ("RADARR_API_KEY", "secret", "", ()),
    "SONARR_URL": ("SONARR_URL", "url", "", ()),
    "SONARR_API_KEY": ("SONARR_API_KEY", "secret", "", ()),
    "SEERR_URL": ("SEERR_URL", "url", "", ("OVERSEERR_URL",)),
    "SEERR_API_KEY": ("SEERR_API_KEY", "secret", "", ("OVERSEERR_API_KEY",)),
    "UNRAID_URL": ("UNRAID_URL", "url", "", ()),
    "UNRAID_API_KEY": ("UNRAID_API_KEY", "secret", "", ()),
    "CAPACITY_PATHS": ("CAPACITY_PATHS", "list", [], ()),
    "WALK_PATHS": ("WALK_PATHS", "pairs", [], ()),
    "WALK_AFTER_REFRESH": ("WALK_AFTER_REFRESH", "bool", True, ()),
    "DISPLAY_PATHS": ("DISPLAY_PATHS", "pairs", [], ()),
    "RECLAIM_READ_ONLY": ("READ_ONLY", "bool", False, ()),
    "RECLAIM_USER": ("AUTH_USER", "str", "", ()),
    "RECLAIM_PASSWORD": ("AUTH_PASSWORD", "password", "", ()),
    "RECLAIM_REFRESH_HOUR": ("REFRESH_HOUR", "int", 4, ()),
    "RECLAIM_STALE_HOURS": ("STALE_HOURS", "int", 20, ()),
}
SECRET_KINDS = ("secret", "password")


class Invalid(ValueError):
    pass


def parse(kind, raw):
    """Env strings and saved JSON values -> the typed value. Raises Invalid with a readable reason."""
    if kind in ("str", "secret", "password"):
        return raw if isinstance(raw, dict) else str(raw or "").strip()
    if kind == "url":
        v = str(raw or "").strip().rstrip("/")
        if v and not v.lower().startswith(("http://", "https://")):
            raise Invalid("must start with http:// or https://")
        return v
    if kind == "bool":
        return raw if isinstance(raw, bool) else str(raw).strip().lower() in ("1", "true", "yes", "on")
    if kind == "int":
        try:
            return int(raw)
        except (TypeError, ValueError):
            raise Invalid("must be a whole number")
    if kind == "list":
        items = raw if isinstance(raw, list) else str(raw or "").split(",")
        return [str(x).strip().rstrip("/") or "/" for x in items if str(x).strip()]
    if kind == "pairs":
        out = []
        items = raw if isinstance(raw, list) else [p.split("=", 1) for p in str(raw or "").split(",") if "=" in p]
        for pair in items:
            if len(pair) != 2 or not str(pair[0]).strip() or not str(pair[1]).strip():
                raise Invalid("each row needs both paths")
            a, b = (str(x).strip().rstrip("/") or "/" for x in pair)
            if not a.startswith("/") or not b.startswith("/"):
                raise Invalid(f"paths must be absolute: {a} = {b}")
            out.append((a, b))
        return out
    raise Invalid(f"unknown kind {kind}")


def env_source(key):
    """The environment variable that sets this key, if any (it then wins and is locked in the UI)."""
    for name in (key, *FIELDS[key][3]):
        if os.environ.get(name, "").strip() != "":
            return name
    return None


def load_saved():
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_saved(data):
    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(SETTINGS_FILE)


def effective(saved=None):
    """{key: typed value} after precedence. Bad env/saved values fall back to the default."""
    saved = load_saved() if saved is None else saved
    out = {}
    for key, (_attr, kind, default, _alt) in FIELDS.items():
        src = env_source(key)
        raw = os.environ[src] if src else saved.get(key, default)
        try:
            out[key] = parse(kind, raw)
        except Invalid:
            out[key] = default
    return out


def reload():
    for key, value in effective().items():
        globals()[FIELDS[key][0]] = value
    if DEMO:                      # demo data is look-don't-touch
        globals()["READ_ONLY"] = True


# ----------------------------------------------------------- password hashing
def hash_password(pw):
    salt = secrets.token_bytes(16)
    it = 200_000
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, it)
    return {"algo": "pbkdf2_sha256", "iter": it, "salt": base64.b64encode(salt).decode(),
            "hash": base64.b64encode(dk).decode()}


def check_password(pw):
    stored = AUTH_PASSWORD
    if isinstance(stored, dict):          # set in the app: hashed
        try:
            dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(stored["salt"]), int(stored["iter"]))
            return hmac.compare_digest(dk, base64.b64decode(stored["hash"]))
        except (KeyError, ValueError):
            return False
    return bool(stored) and hmac.compare_digest(pw.encode(), str(stored).encode())


def auth_on():
    return bool(AUTH_USER and AUTH_PASSWORD)


def client_id():
    """Stable id this install presents to plex.tv (sign-in)."""
    saved = load_saved()
    if not saved.get("_client_id"):
        saved["_client_id"] = "reclaim-" + secrets.token_hex(8)
        write_saved(saved)
    return saved["_client_id"]


def needs_setup():
    return not PLEX_URL and not DEMO


def summary():
    """What's configured, for the UI and the startup log. No secrets."""
    return {
        "plex": bool(PLEX_URL),
        "plex_token": bool(PLEX_TOKEN),
        "tautulli": bool(TAUTULLI_URL and TAUTULLI_API_KEY),
        "radarr": bool(RADARR_URL and RADARR_API_KEY),
        "sonarr": bool(SONARR_URL and SONARR_API_KEY),
        "seerr": bool(SEERR_URL and SEERR_API_KEY),
        "capacity": "unraid" if (UNRAID_URL and UNRAID_API_KEY) else ("disk" if CAPACITY_PATHS else None),
        "walk": "local" if WALK_PATHS else None,
        "read_only": READ_ONLY,
        "auth": auth_on(),
        "needs_setup": needs_setup(),
        "demo": DEMO,
    }


# module attributes, filled now and on every save
PLEX_URL = PLEX_TOKEN = TAUTULLI_URL = TAUTULLI_API_KEY = ""
RADARR_URL = RADARR_API_KEY = SONARR_URL = SONARR_API_KEY = SEERR_URL = SEERR_API_KEY = ""
UNRAID_URL = UNRAID_API_KEY = AUTH_USER = ""
AUTH_PASSWORD = ""
CAPACITY_PATHS, WALK_PATHS, DISPLAY_PATHS = [], [], []
WALK_AFTER_REFRESH, READ_ONLY = True, False
# RECLAIM_DEMO=1: serve the made-up library from tools/demo_data.py, read-only, talking to nothing
DEMO = os.environ.get("RECLAIM_DEMO", "").strip().lower() in ("1", "true", "yes", "on")
REFRESH_HOUR, STALE_HOURS = 4, 20
reload()
