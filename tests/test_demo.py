"""The demo library builds and the model reads it the way the screenshots show. Run: python tests/test_demo.py (no deps)."""
import gzip
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
tmp = Path(tempfile.mkdtemp(prefix="reclaim-demo-"))
os.environ["RECLAIM_DATA"] = str(tmp)
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "backend"))
import demo_data  # noqa: E402
from model import Model  # noqa: E402

demo_data.write(tmp)
raw = json.load(gzip.open(tmp / "raw.json.gz", "rt", encoding="utf-8"))
walk = json.load(gzip.open(tmp / "walk.json.gz", "rt", encoding="utf-8"))
m = Model(raw, walk)
titles = list(m.titles.values())

assert len([t for t in titles if t["kind"] == "movie"]) == 150 and len([t for t in titles if t["kind"] == "show"]) == 30
never = [t for t in titles if not t["users"]]
assert 30 < len(never) < 120, len(never)                                  # a real "never played" story
assert sum(1 for t in titles if t["requests"]) >= 30                     # requests matched to titles
assert sum(1 for t in titles if t.get("arr")) > 150                      # radarr/sonarr matched by folder or id
assert any(len(t.get("versions") or []) == 2 for t in titles)            # 4K + 1080p copies exist
assert m.unindexed and any(g["title"] is None for g in m.unindexed)      # files Plex doesn't know, some orphaned
assert m.array["free"] < m.array["total"] * 0.2                          # the array is nearly full
assert {u["name"] for u in m.users.values()} >= {"alex", "jordan", "sam"}
assert demo_data.build(7)[0]["movies"][0]["title"] == demo_data.build(7)[0]["movies"][0]["title"]   # deterministic
print(f"demo ok: {len(titles)} titles, {len(never)} never played, {len(m.unindexed)} unindexed groups")
