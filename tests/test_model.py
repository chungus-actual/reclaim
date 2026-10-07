"""Model invariants on synthetic data. Run: python tests/test_model.py (no deps)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from model import Model, classify  # noqa: E402

GB = 10**9


def media(*files, res="1080", mid=1):
    return [{"id": mid, "res": res, "vcodec": "h264", "acodec": "ac3", "bitrate": 1, "container": "mkv", "height": 1080,
             "parts": [{"file": f, "size": s} for f, s in files]}]


def raw():
    return {
        "sections": [{"id": "1", "type": "movie", "title": "Movies", "roots": ["/data/Movies"]},
                     {"id": "2", "type": "show", "title": "TV", "roots": ["/data/TV"]}],
        "movies": [
            {"key": "m1", "section": "1", "title": "Alpha", "year": 2001, "added": 1, "duration": 6_000_000, "rating": None,
             "content": None, "genres": [], "thumb": None, "ids": {"tmdb": "11"},
             "media": media(("/data/Movies/Alpha (2001)/a.mkv", 10 * GB)) + media(("/data/Movies/Alpha (2001)/a4k.mkv", 40 * GB), res="4k", mid=2)},
            {"key": "m2", "section": "1", "title": "Beta", "year": 2005, "added": 1, "duration": 5_000_000, "rating": None,
             "content": None, "genres": [], "thumb": None, "ids": {"tmdb": "22"},
             "media": media(("/data/Movies/Beta (2005)/b.mkv", 5 * GB))},
        ],
        "shows": [{"key": "s1", "section": "2", "title": "Show", "year": 2010, "added": 1, "rating": None, "content": None,
                   "genres": [], "thumb": None, "ids": {"tvdb": "99"}, "leafs": 3, "seasons": 1}],
        "seasons": [{"key": "se1", "show": "s1", "index": 1, "title": "Season 1", "thumb": None}],
        "episodes": [
            # e1 + e2 share one multi-episode file: counted once
            {"key": "e1", "season": "se1", "show": "s1", "index": 1, "sindex": 1, "title": "One", "added": 1, "duration": 1_800_000,
             "media": media(("/data/TV/Show/S01E01E02.mkv", 2 * GB))},
            {"key": "e2", "season": "se1", "show": "s1", "index": 2, "sindex": 1, "title": "Two", "added": 1, "duration": 1_800_000,
             "media": media(("/data/TV/Show/S01E01E02.mkv", 2 * GB))},
            {"key": "e3", "season": "se1", "show": "s1", "index": 3, "sindex": 1, "title": "Three", "added": 1, "duration": 1_800_000,
             "media": media(("/data/TV/Show/S01E03.mkv", 1 * GB))},
        ],
        "users": [{"id": 100, "name": "owner", "admin": True, "home": False},
                  {"id": 200, "name": "friend", "admin": False, "home": False}],
        "tautulli_history": [
            # matched by rating key
            dict(rating_key="m1", media_type="movie", user_id=200, date=2000, started=2000, stopped=2100, play_duration=3600,
                 percent_complete=95, watched_status=1, title="Alpha", grandparent_title=None, year=2001,
                 parent_media_index=None, media_index=None, parent_rating_key=None, grandparent_rating_key=None),
            # stale rating key -> recovered by title+year
            dict(rating_key="old-beta", media_type="movie", user_id=100, date=3000, started=3000, stopped=3050, play_duration=60,
                 percent_complete=2, watched_status=0, title="Beta", grandparent_title=None, year=2005,
                 parent_media_index=None, media_index=None, parent_rating_key=None, grandparent_rating_key=None),
            # episode with unknown key -> show by grandparent key, season by index
            dict(rating_key="gone-ep", media_type="episode", user_id=200, date=4000, started=4000, stopped=4500, play_duration=1500,
                 percent_complete=100, watched_status=1, title="x", grandparent_title="Show", year=2010,
                 parent_media_index=1, media_index=9, parent_rating_key=None, grandparent_rating_key="s1"),
            dict(rating_key="e3", media_type="episode", user_id=200, date=5000, started=5000, stopped=5600, play_duration=1700,
                 percent_complete=100, watched_status=1, title="Three", grandparent_title="Show", year=2010,
                 parent_media_index=1, media_index=3, parent_rating_key="se1", grandparent_rating_key="s1"),
            # unmatched
            dict(rating_key="zzz", media_type="movie", user_id=200, date=6000, started=6000, stopped=6000, play_duration=1,
                 percent_complete=1, watched_status=0, title="Nope", grandparent_title=None, year=1999,
                 parent_media_index=None, media_index=None, parent_rating_key=None, grandparent_rating_key=None),
        ],
        "plex_history": [
            {"key": "m2", "type": "movie", "account": 1, "at": 1000, "section": "1"},   # before tautulli: owner (acct 1)
            {"key": "m2", "type": "movie", "account": 1, "at": 2500, "section": "1"},   # after tautulli start: ignored
        ],
        "requests": [{"id": 1, "type": "movie", "status": 2, "at": "2020-01-01T00:00:00Z", "plex_id": 200, "by": "friend",
                      "tmdb": 22, "tvdb": None, "rating_key": None, "seasons": []}],
        "arr": {"radarr": [{"id": 7, "title": "Alpha", "year": 2001, "tmdb": 11, "path": "/movies/Alpha (2001)", "monitored": True,
                            "has_file": True, "profile": 1, "size": 1, "file": "a.mkv", "added": None}],
                "radarr_profiles": {1: "Best"}, "radarr_mm": {"autoUnmonitorPreviouslyDownloadedMovies": False}},
        "array": None,
    }


def test_sizes_and_dedupe():
    m = Model(raw())
    assert m.titles["m1"]["size"] == 50 * GB, "both versions count"
    assert m.titles["s1"]["size"] == 3 * GB, "multi-episode file counted once"
    assert m.multi_ep_bytes == 2 * GB
    assert m.seasons["se1"]["size"] == 3 * GB
    assert m.titles["m1"]["res"] == "4K"


def test_plays():
    m = Model(raw())
    assert m.unmatched_plays == 1
    assert m.titles["m1"]["users"][200][:2] == (1, 1)
    beta = m.titles["m2"]["users"]
    assert beta[100][0] == 2, "title-year recovery + pre-tautulli owner view"
    assert beta[100][1] == 1, "only the Plex view counts as finished"
    assert m.pre_tautulli_plays == 1
    show = m.titles["s1"]["users"][200]
    assert show[0] == 2 and show[5] == 1, "unknown-episode play attributed to show; distinct eps finished = 1 (e3)"
    assert m.titles["s1"]["eps_seen"] == 1


def test_arr_and_requests():
    m = Model(raw())
    assert m.titles["m1"]["arr"]["id"] == 7 and m.titles["m1"]["arr"]["profile"] == "Best"
    assert m.titles["m2"]["arr"] is None
    assert m.arr_safety == {"radarr": False}
    assert [r["uid"] for r in m.titles["m2"]["requests"]] == [200], "request matched by tmdb"


def test_walk():
    walk = {"roots": ["/data/Movies", "/data/TV"], "files": [
        ["/data/Movies/Alpha (2001)/a.mkv", 10 * GB],
        ["/data/Movies/Alpha (2001)/a4k.mkv", 40 * GB],
        ["/data/Movies/Alpha (2001)/Alpha.iso", 30 * GB],
        ["/data/Movies/Alpha (2001)/a.en.srt", 50_000],
        ["/data/Movies/Gamma (1990)/gamma.iso", 20 * GB],
        ["/data/Movies/Beta (2005)/b.mkv", 5 * GB],
        ["/data/TV/Show/S01E01E02.mkv", 2 * GB],
        ["/data/TV/Show/.S01E03.mkv.Ab12Cd", 1 * GB],
    ]}
    m = Model(raw(), walk)
    assert m.titles["m1"]["xbytes"] == 30 * GB, "iso next to an indexed movie; sidecar excluded"
    assert m.titles["s1"]["xbytes"] == 1 * GB
    gamma = next(g for g in m.unindexed if g["folder"].endswith("Gamma (1990)"))
    assert gamma["title"] is None
    assert m.missing_on_disk == 1, "S01E03.mkv was not in the walk"
    assert dict(m.unindexed_by_cat)["temp"][1] == 1 * GB


def test_extras():
    making = "/data/Movies/Beta (2005)/Featurettes/Making Beta.mkv"
    blooper = "/data/TV/Show/Season 1/Bloopers-Short.avi"
    orphan = "/data/Movies/Gamma (1990)/Featurettes/g.mkv"
    walk = {"roots": ["/data/Movies", "/data/TV"], "files": [
        ["/data/Movies/Beta (2005)/b.mkv", 5 * GB],
        [making, 300 * 10**6],
        ["/data/Movies/Beta (2005)/Featurettes/Making Beta.en.srt", 40_000],
        ["/data/Movies/Beta (2005)/stray.mkv", 1 * GB],
        [blooper, 200 * 10**6],
        [orphan, 100 * 10**6],
    ]}
    before = Model(raw(), walk)
    assert before.extra_candidates() == ["m2", "s1", "se1"], "titles with unaccounted video, plus the show's seasons"
    assert making in {f[0] for g in before.unindexed for f in g["files"]}
    extras = {"m2": [{"key": "c1", "title": "Making Beta", "subtype": "behindTheScenes", "files": [[making, 300 * 10**6]]}],
              "se1": [{"key": "c2", "title": "Bloopers", "subtype": "short", "files": [[blooper, 200 * 10**6]]}],
              "m9": [{"key": "c3", "title": "gone", "subtype": "trailer", "files": [[orphan, 100 * 10**6]]}]}
    m = Model(raw(), walk, extras=extras)
    left = {f[0] for g in m.unindexed for f in g["files"]}
    assert making not in left and blooper not in left, "Plex-indexed extras aren't unindexed"
    assert "/data/Movies/Beta (2005)/stray.mkv" in left and orphan in left, "unknown video and a deleted title's extra stay"
    assert m.extras_on_disk == [2, 500 * 10**6]
    assert m.titles["m2"]["xbytes"] == 1 * GB and m.titles["m2"]["size"] == 5 * GB, "extras are neither loose nor title bytes"
    assert m.extra_candidates() == before.extra_candidates(), "a lookup's answer doesn't change who gets asked"
    assert m.detail("s1")["extras"] == [(blooper, 200 * 10**6, "short")], "season-level extra belongs to the show"


def test_classify():
    assert classify("/data/Movies/X/.X 2024.mkv.Frqk5C", 1) == "temp"
    assert classify("/data/TV/A/.fuse_hidden00006a0c00000004", 1) == "temp"
    assert classify("/data/Movies/X/BDMV/STREAM/00001.m2ts", 1) == "disc"
    assert classify("/data/TV/D/DR0ON7~D/ep.avi", 1) == "alias"
    assert classify("/data/TV/D/ep.avi", 1) == "video"
    assert classify("/data/TV/D/ep.en.srt", 1) == "sidecar"


def test_client_payload():
    m = Model(raw())
    c = m.client()
    t = {x["k"]: x for x in c["titles"]}
    assert t["m1"]["v"] == 2 and t["s1"]["e"] == 3 and t["s1"]["es"] == 1
    assert c["space"]["indexed"] == 58 * GB
    d = m.detail("s1")
    assert d["seasons"][0]["eps_seen"] == 1 and d["seasons"][0]["viewers"] == 1


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("ok", name)
    print(f"{n} passed")
