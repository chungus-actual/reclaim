"""Downgrade logic on synthetic data. Run: python tests/test_downgrade.py (no deps)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
import downgrade as D  # noqa: E402

GB = 10**9


def q(name, res, qid):
    return {"quality": {"id": qid, "name": name, "resolution": res}, "items": [], "allowed": True}


BASE = {"name": "Any", "upgradeAllowed": True, "cutoff": 1, "formatItems": [{"format": 1, "name": "Anime", "score": 500}],
        "items": [q("Unknown", 0, 0), q("SDTV", 480, 1), q("HDTV-720p", 720, 4),
                  {"name": "WEB 720p", "id": 1002, "allowed": True, "items": [q("WEBDL-720p", 720, 5)]},
                  q("Bluray-720p", 720, 6), q("Bluray-1080p", 1080, 7), q("Bluray-1080p Remux", 1080, 20), q("Bluray-2160p", 2160, 19)]}


def names(p):
    return [i.get("name") or i["quality"]["name"] for i in p["items"]]


def test_profile_ranks_big_qualities_lowest():
    p = D.build_profile(BASE, 720)
    n = names(p)
    assert n[:3] == ["Bluray-1080p", "Bluray-1080p Remux", "Bluray-2160p"], n
    assert n[-1] == "Bluray-720p"
    allowed = [x for x, i in zip(n, p["items"]) if i["allowed"]]
    assert allowed == ["HDTV-720p", "WEB 720p", "Bluray-720p"]
    assert all(x["allowed"] for x in p["items"][n.index("WEB 720p")]["items"]), "group members follow the group"
    assert p["upgradeAllowed"] is False and p["cutoff"] == 6 and p["name"] == "Reclaim ↓720p"
    assert p["formatItems"] == [{"format": 1, "name": "Anime", "score": 0}]
    p = D.build_profile(BASE, 1080)
    assert names(p)[:2] == ["Bluray-1080p Remux", "Bluray-2160p"], "remux ranks with the big ones"
    assert [x for x, i in zip(names(p), p["items"]) if i["allowed"]] == ["Bluray-1080p"]


def rel(title, name, res, size, rejections=(), weight=0, langs=("English",), full=True, season=2):
    return {"guid": title + str(size), "indexerId": 1, "indexer": "x", "title": title, "size": size,
            "quality": {"quality": {"name": name, "resolution": res}}, "rejections": list(rejections),
            "releaseWeight": weight, "languages": [{"name": l} for l in langs], "fullSeason": full,
            "seasonNumber": season, "age": 10, "protocol": "usenet"}


def test_candidates():
    rels = [
        rel("Show.S02.720p.BluRay-GOOD", "Bluray-720p", 720, 6 * GB, ["Existing file meets cutoff: SDTV"], weight=5),
        rel("Show.S02.720p.BluRay-BEST", "Bluray-720p", 720, 7 * GB, ["4.7 GB is smaller than minimum allowed 10.9 GB (for 13x 650min)"], weight=2),
        rel("Show.S02.720p.DUAL.WEB-DL", "WEBDL-720p", 720, 5 * GB, weight=1),                 # multi-audio: ranked after
        rel("Show.S02.EXTRAS.720p", "HDTV-720p", 720, 4 * GB, weight=0),                     # extras: blocked
        rel("Show.S02.720p.tiny", "Bluray-720p", 720, 70 * 10**6, weight=0),                 # junk: blocked
        rel("Show.S02.720p.old", "Bluray-720p", 720, 6 * GB, ["Older than configured retention"], weight=0),
        rel("Show.S02.720p.BIG", "Bluray-720p", 720, 25 * GB, weight=0),                     # saves < 25%: not recommended
        rel("Show.S02E01.720p", "Bluray-720p", 720, 1 * GB, full=False),                     # single episode: not a pack
        rel("Show.S02.1080p", "Bluray-1080p", 1080, 9 * GB),                                 # wrong tier
    ]
    c = D.candidates(rels, 720, 28 * GB, 650, season=2, app="Sonarr")
    by = {x["title"]: x for x in c}
    assert "Show.S02E01.720p" not in by and "Show.S02.1080p" not in by
    assert not by["Show.S02.EXTRAS.720p"]["ok"]
    assert not by["Show.S02.720p.tiny"]["ok"]
    assert not by["Show.S02.720p.old"]["ok"]
    assert by["Show.S02.720p.BluRay-BEST"]["warnings"] == ["under your Sonarr size rule (10.9 GB)"]
    assert by["Show.S02.720p.DUAL.WEB-DL"]["multi"]
    rec = [x["title"] for x in c if x["recommended"]]
    assert rec == ["Show.S02.720p.BluRay-BEST"], rec
    anime = D.candidates(rels, 720, 28 * GB, 650, season=2, app="Sonarr", prefer_multi=True)
    assert [x["title"] for x in anime if x["recommended"]] == ["Show.S02.720p.DUAL.WEB-DL"]


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("ok", name)
    print(f"{n} passed")
