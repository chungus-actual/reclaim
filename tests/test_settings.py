"""Settings layer: parsing, env precedence, saving, password hashing. Run: python tests/test_settings.py"""
import os
import sys
import tempfile
from pathlib import Path

DATA = tempfile.mkdtemp(prefix="reclaim-test-")
os.environ["RECLAIM_DATA"] = DATA
for k in list(os.environ):
    if k.startswith(("PLEX_", "TAUTULLI_", "RADARR_", "SONARR_", "SEERR_", "OVERSEERR_", "UNRAID_", "WALK_", "CAPACITY_",
                     "DISPLAY_")) or k in ("RECLAIM_USER", "RECLAIM_PASSWORD", "RECLAIM_READ_ONLY"):
        del os.environ[k]
os.environ["RECLAIM_ENV_FILE"] = os.path.join(DATA, "none.env")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import config as C  # noqa: E402
import settings  # noqa: E402


def test_parse():
    assert C.parse("url", " http://x:1/ ") == "http://x:1"
    for bad in ("x:1", "ftp://x"):
        try:
            C.parse("url", bad)
            raise AssertionError(bad)
        except C.Invalid:
            pass
    assert C.parse("pairs", "/data/movies=/media/movies, /data/tv/=/media/tv") == [("/data/movies", "/media/movies"), ("/data/tv", "/media/tv")]
    assert C.parse("pairs", [["/a", "/b"]]) == [("/a", "/b")]
    try:
        C.parse("pairs", [["relative", "/b"]])
        raise AssertionError("relative path accepted")
    except C.Invalid:
        pass
    assert C.parse("list", "/a, /b/,") == ["/a", "/b"]
    assert C.parse("bool", "yes") is True and C.parse("bool", False) is False


def test_first_run_and_save():
    assert C.needs_setup()
    res = settings.save({"PLEX_URL": "http://plex:32400/", "PLEX_TOKEN": "tok", "WALK_PATHS": [["/data/m", "/media/m"]]})
    assert res["ok"], res
    assert C.PLEX_URL == "http://plex:32400" and C.PLEX_TOKEN == "tok" and C.WALK_PATHS == [("/data/m", "/media/m")]
    assert not C.needs_setup()
    # a blank secret keeps the saved one; clear removes it
    settings.save({"PLEX_TOKEN": ""})
    assert C.PLEX_TOKEN == "tok"
    settings.save({}, clear=["PLEX_TOKEN"])
    assert C.PLEX_TOKEN == ""
    p = settings.payload()
    tok = next(f for g in p["groups"] for f in g["fields"] if f["key"] == "PLEX_TOKEN")
    assert "value" not in tok and tok["set"] is False, "secrets never go to the page"
    bad = settings.save({"PLEX_URL": "plex:32400"})
    assert not bad["ok"] and "PLEX_URL" in bad["errors"]
    assert C.PLEX_URL == "http://plex:32400", "a rejected save changes nothing"


def test_env_wins_and_locks():
    os.environ["RADARR_URL"] = "http://from-env:7878"
    try:
        settings.save({"RADARR_URL": "http://from-ui:7878"})
        assert C.RADARR_URL == "http://from-env:7878"
        f = next(f for g in settings.payload()["groups"] for f in g["fields"] if f["key"] == "RADARR_URL")
        assert f["locked"] == "RADARR_URL"
    finally:
        del os.environ["RADARR_URL"]
        C.reload()
    os.environ["OVERSEERR_URL"] = "http://legacy:5055"
    try:
        C.reload()
        assert C.SEERR_URL == "http://legacy:5055", "old variable name still works"
    finally:
        del os.environ["OVERSEERR_URL"]
        C.reload()


def test_password():
    assert not C.auth_on()
    r = settings.save({"RECLAIM_USER": "me"})
    assert not r["ok"], "user without password is refused"
    assert settings.save({"RECLAIM_USER": "me", "RECLAIM_PASSWORD": "s3cret"})["ok"]
    assert C.auth_on() and isinstance(C.AUTH_PASSWORD, dict), "stored hashed"
    assert "s3cret" not in Path(DATA, "settings.json").read_text()
    assert C.check_password("s3cret") and not C.check_password("nope")
    assert settings.save({"RECLAIM_USER": ""})["ok"]
    assert not C.auth_on(), "clearing the user turns the login off"


def test_browse():
    d = settings.browse(DATA)
    assert d["path"] == os.path.normpath(DATA) and "dirs" in d
    assert settings.browse(os.path.join(DATA, "nope"))["error"]


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("ok", name)
    print(f"{n} passed")
